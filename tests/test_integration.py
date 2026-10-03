"""End-to-end tests: real harness, real Playwright browser, real mock company app.

Only the model is replaced, by a scripted policy that reads the same tool
results Claude would see. This proves the plumbing (observation, refs,
downloads, secrets, approvals, guards, verification, repair, reporting)
without API calls; model judgment is exercised by live runs.
"""

import json

import pytest
from conftest import BASE, chaos, find_ref, mock_state

from mock_env import data
from worker.llm import ScriptedLLM, last_tool_text
from worker.orchestrator import run_task
from worker.user import AutoUser

API_HEADERS = {"X-API-Key": "{{secret:erp_api_key}}"}


def expect(text: str, *needles: str) -> str:
    for needle in needles:
        assert needle in text, f"expected {needle!r} in:\n{text[:3000]}"
    return text


def fill_bill_form(text: str, amount: str) -> list:
    fields = [
        (r'select "Vendor"', "Acme Cloud Services"),
        (r'textbox "Invoice number"', "ACME-2026-0917"),
        (r'textbox "Invoice date"', "30/09/2026"),
        (r'textbox "Due date"', "30/10/2026"),
        (r'select "Currency"', "usd"),  # option labels match case-insensitively
        (r'textbox "Amount"', amount),
        (r'select "Category"', "Software & Cloud"),
    ]
    return [("browser_fill", {"fields": [{"ref": find_ref(text, p), "value": v} for p, v in fields],
                              "submit_ref": find_ref(text, r'button "Save bill"')})]


@pytest.mark.parametrize("budget", [None, 2200], ids=["full-context", "token-budgeted"])
def test_invoice_to_erp_with_validation_error_and_ambiguous_502(env, budget):
    settings, vault = env
    chaos(flaky_submit=True)  # first valid submit is committed but the user sees a 502

    worker = ScriptedLLM(budget=budget, steps=[
        lambda m: [("update_plan", {"steps": [
            {"title": "Find latest Acme invoice in mail", "status": "in_progress"},
            {"title": "Extract amount and due date", "status": "pending"},
            {"title": "Enter bill in ERP", "status": "pending"},
            {"title": "Confirm the saved bill", "status": "pending"}]})],
        lambda m: [("browser_navigate", {"url": f"{BASE}/mail?q=acme"})],
        lambda m: [("browser_click", {"ref": find_ref(last_tool_text(m), r'link "Your Acme Cloud invoice ACME-2026-0917')})],
        lambda m: [("browser_click", {"ref": find_ref(last_tool_text(m), r'link "ACME-2026-0917.pdf"')})],
        lambda m: expect(last_tool_text(m), "downloads/ACME-2026-0917.pdf") and
                  [("read_file", {"path": "downloads/ACME-2026-0917.pdf"})],
        lambda m: expect(last_tool_text(m), "Total due USD 4,812.50", "Payment due: 2026-10-30") and [
            ("remember", {"facts": [
                {"key": "invoice_amount", "value": "USD 4812.50", "source": "ACME-2026-0917.pdf"},
                {"key": "due_date", "value": "2026-10-30", "source": "ACME-2026-0917.pdf"}]}),
            ("read_file", {"path": "vendor_categories.csv"})],
        lambda m: [("browser_navigate", {"url": f"{BASE}/erp/bills/new"})],
        lambda m: [("browser_fill", {"fields": [
            {"ref": find_ref(last_tool_text(m), r'textbox "Username"'), "value": "agent.worker"},
            {"ref": find_ref(last_tool_text(m), r'password "Password"'), "value": "{{secret:erp_password}}"}],
            "submit_ref": find_ref(last_tool_text(m), r'button "Sign in"')})],
        # Login redirects back to the form. First attempt uses a thousands separator -> validation error.
        lambda m: fill_bill_form(expect(last_tool_text(m), "# New bill"), "4,812.50"),
        lambda m: fill_bill_form(expect(last_tool_text(m), "thousands separators"), "4812.50"),
        # 502: the write may or may not have happened -> check instead of resubmitting.
        lambda m: expect(last_tool_text(m), "HTTP 502", "outcome is unknown") and
                  [("browser_navigate", {"url": f"{BASE}/erp/bills?q=ACME-2026-0917"})],
        lambda m: [("browser_click", {"ref": find_ref(last_tool_text(m), r'link "AP-1043"')})],
        lambda m: expect(last_tool_text(m), "Invoice number: ACME-2026-0917", "USD 4,812.50", "30/10/2026") and [
            ("remember", {"facts": [{"key": "bill_id", "value": "AP-1043", "source": "ERP bill page"}]}),
            ("finish", {"status": "success", "summary": "Entered Acme invoice ACME-2026-0917 as bill AP-1043.",
                        "result": {"amount": "USD 4812.50", "due_date": "2026-10-30", "bill_id": "AP-1043"},
                        "evidence": [{"claim": "Bill AP-1043 exists", "source": f"{BASE}/erp/bills/AP-1043"}],
                        "self_check": "Opened the bill page after the 502 and compared every field."})],
    ])
    verifier = ScriptedLLM([
        lambda m: [("http_request", {"method": "GET", "url": f"{BASE}/erp/api/bills?invoice_number=ACME-2026-0917",
                                     "headers": API_HEADERS})],
        # The verifier is read-only: a write attempt must be refused by the policy gate.
        lambda m: expect(last_tool_text(m), '"AP-1043"', '"due_date": "2026-10-30"') and
                  [("http_request", {"method": "POST", "url": f"{BASE}/erp/api/bills", "json_body": {}})],
        lambda m: expect(last_tool_text(m), "not permitted in this mode") and [("submit_verdict", {
            "verdict": "verified", "summary": "Exactly one bill, fields match the PDF.",
            "checks": [{"claim": "Bill exists once", "observed": "API returns AP-1043 only", "passed": True}]})],
    ])

    result = run_task("Find the latest invoice from Acme Cloud and enter it into the ERP.", settings,
                      AutoUser(), llm=worker, verifier_llm=verifier, vault=vault)

    assert result.status == "completed_verified", result.summary
    acme = [b for b in mock_state()["bills"] if b["invoice_number"] == "ACME-2026-0917"]
    assert len(acme) == 1 and acme[0]["amount"] == 4812.5 and acme[0]["due_date"] == "2026-10-30"
    assert result.memory["invoice_amount"]["value"] == "USD 4812.50"
    events = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert data.ERP_PASSWORD not in events and data.ERP_API_KEY not in events
    assert list((result.run_dir / "screenshots").glob("*.png"))
    assert "independently verified" in result.report_path.read_text(encoding="utf-8")
    if budget:  # every request stayed within budget, and older steps were carried as digests + memory
        sizes = [worker.count_tokens(json.dumps(view, default=str)) for view in worker.seen]
        assert max(sizes) <= budget, sizes
        late = json.dumps(worker.seen[-1], default=str)
        assert "Earlier steps (summarised)" in late and "invoice_amount = USD 4812.50" in late


def test_outbound_email_requires_approval_and_decline_is_respected(env):
    settings, vault = env
    worker = ScriptedLLM([
        lambda m: [("browser_navigate", {"url": f"{BASE}/mail/compose?to=orders@initech.example&subject=IS-3302"})],
        lambda m: [("browser_fill", {
            "fields": [{"ref": find_ref(last_tool_text(m), r'textbox "Message"'), "value": "Received, thanks."}],
            "submit_ref": find_ref(last_tool_text(m), r'button "Send"')})],
        lambda m: expect(last_tool_text(m), "did NOT approve") and [("finish", {
            "status": "blocked", "summary": "Sending was not approved.", "evidence": [], "self_check": "n/a"})],
    ])
    user = AutoUser(approve_all=False)
    result = run_task("Email Initech to confirm receipt of IS-3302.", settings, user, llm=worker, vault=vault)

    assert result.status == "blocked"
    assert len(user.approvals) == 1
    assert "orders@initech.example" in user.approvals[0] and "Received, thanks." in user.approvals[0]
    assert mock_state()["sent"] == []


def test_failed_verification_triggers_repair_via_api(env):
    settings, vault = env
    bill = {"vendor_id": "V-1004", "invoice_number": "IS-3302", "invoice_date": "2026-09-11",
            "due_date": "2026-10-11", "currency": "USD", "amount": 389.99, "category": "Office Supplies"}
    worker = ScriptedLLM([
        # Premature claim of success...
        lambda m: [("finish", {"status": "success", "summary": "Entered IS-3302.", "evidence": [], "self_check": "none"})],
        # ...the verifier's feedback arrives; the worker repairs through the API.
        lambda m: expect(last_tool_text(m), "did NOT confirm") and [
            ("http_request", {"method": "POST", "url": f"{BASE}/erp/api/bills", "headers": API_HEADERS, "json_body": bill})],
        lambda m: expect(last_tool_text(m), "HTTP 201") and [("finish", {
            "status": "success", "summary": "Entered IS-3302 via the ERP API.", "result": {"bill_id": "AP-1043"},
            "evidence": [{"claim": "API returned 201", "source": "POST /erp/api/bills"}], "self_check": "201 response"})],
    ])
    verdicts = iter([
        {"verdict": "not_verified", "summary": "No bill for IS-3302.",
         "checks": [{"claim": "Bill exists", "observed": "API returned no bills", "passed": False}]},
        {"verdict": "verified", "summary": "AP-1043 matches.",
         "checks": [{"claim": "Bill exists", "observed": "AP-1043", "passed": True}]},
    ])
    verifier = ScriptedLLM([lambda m: [("submit_verdict", next(verdicts))]] * 2)

    result = run_task("Enter Initech invoice IS-3302.", settings, AutoUser(), llm=worker, verifier_llm=verifier,
                      vault=vault)

    assert result.status == "completed_verified"
    assert [v["verdict"] for v in result.verification] == ["not_verified", "verified"]
    assert any(b["invoice_number"] == "IS-3302" for b in mock_state()["bills"])


def test_step_budget_and_repetition_guard(env):
    settings, vault = env
    settings.max_steps = 5
    worker = ScriptedLLM([lambda m: [("browser_snapshot", {})]] * 5)
    result = run_task("Loop forever.", settings, AutoUser(), llm=worker, vault=vault)

    assert result.status == "failed" and "step budget" in result.summary
    events = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "same browser_snapshot call 3 times" in events
    assert "steps remain in your budget" in events


def test_snapshot_rendering(env):
    settings, vault = env
    worker = ScriptedLLM([
        lambda m: [("browser_navigate", {"url": f"{BASE}/erp/login"})],
        lambda m: [("browser_fill", {"fields": [{"ref": find_ref(last_tool_text(m), r'password "Password"'),
                                                  "value": "{{secret:erp_password}}"}]}),
                   ("browser_snapshot", {})],
        lambda m: expect(last_tool_text(m), 'value="********"', "# Sign in") and [
            ("browser_navigate", {"url": "https://example.com"})],
        lambda m: expect(last_tool_text(m), "Blocked by policy") and [
            ("finish", {"status": "failed", "summary": "done", "evidence": [], "self_check": "n/a"})],
    ])
    result = run_task("Inspect.", settings, AutoUser(), llm=worker, vault=vault)
    assert result.status == "failed"
    assert data.ERP_PASSWORD not in (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
