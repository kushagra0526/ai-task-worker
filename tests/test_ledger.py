"""Duplicate protection: the harness, not the prompt, prevents repeating a state change blindly."""

import json

from conftest import BASE, chaos, find_ref, mock_state

from worker.llm import ScriptedLLM, last_tool_text
from worker.orchestrator import run_task
from worker.user import AutoUser

BILL = [(r'select "Vendor"', "Acme Cloud Services"), (r'textbox "Invoice number"', "ACME-2026-0917"),
        (r'textbox "Invoice date"', "30/09/2026"), (r'textbox "Due date"', "30/10/2026"),
        (r'select "Currency"', "USD"), (r'textbox "Amount"', "4812.50"), (r'select "Category"', "Software & Cloud")]


def login(m):
    text = last_tool_text(m)
    return [("browser_fill", {"fields": [{"ref": find_ref(text, r'textbox "Username"'), "value": "agent.worker"},
                                         {"ref": find_ref(text, r'password "Password"'), "value": "{{secret:erp_password}}"}],
                              "submit_ref": find_ref(text, r'button "Sign in"')})]


def submit_bill(m):
    text = last_tool_text(m)
    return [("browser_fill", {"fields": [{"ref": find_ref(text, p), "value": v} for p, v in BILL],
                              "submit_ref": find_ref(text, r'button "Save bill"')})]


def go(path):
    return lambda m: [("browser_navigate", {"url": BASE + path})]


def finish(status):
    return lambda m: [("finish", {"status": status, "summary": "done", "evidence": [], "self_check": "n/a"})]


def test_blind_resubmit_after_unknown_outcome_is_blocked_until_state_is_checked(env):
    settings, vault = env
    settings.verify = False
    chaos(flaky_submit=True)  # the first valid save is committed, but the response is a 502
    seen = []
    worker = ScriptedLLM([
        go("/erp/bills/new"), login, submit_bill,                              # -> 502, outcome unknown
        go("/erp/bills/new"),                                                  # back to the form: not a state check
        lambda m: seen.append(last_tool_text(m)) or submit_bill(m),            # identical resubmit -> blocked
        lambda m: seen.append(last_tool_text(m)) or go("/erp/bills?q=ACME-2026-0917")(m),  # look elsewhere
        go("/erp/bills/new"),
        submit_bill,                                                           # allowed now; server rejects duplicate
        lambda m: seen.append(last_tool_text(m)) or finish("success")(m),
    ])
    result = run_task("Enter the Acme invoice.", settings, AutoUser(approve_all=True), llm=worker, vault=vault)

    assert "Blocked by the harness" in seen[1] and "unknown outcome" in seen[1]
    assert "Duplicate: bill AP-1043 already exists" in seen[2]
    assert len([b for b in mock_state()["bills"] if b["invoice_number"] == "ACME-2026-0917"]) == 1
    assert [e["outcome"] for e in result.ledger] == ["applied", "unknown", "rejected"]  # login, 502 save, dup save
    assert "duplicate_blocked" in (result.run_dir / "events.jsonl").read_text(encoding="utf-8")


def test_identical_resubmit_after_success_requires_approval(env):
    settings, vault = env
    settings.verify = False
    user = AutoUser(approve_all=False)
    worker = ScriptedLLM([
        go("/erp/bills/new"), login, submit_bill,      # applied
        go("/erp/bills/new"), submit_bill,              # identical -> needs approval -> declined
        finish("success"),
    ])
    result = run_task("Enter the Acme invoice.", settings, user, llm=worker, vault=vault)

    assert len(user.approvals) == 1 and user.approvals[0].startswith("POSSIBLE DUPLICATE")
    assert [e["outcome"] for e in result.ledger] == ["applied", "applied"]  # login + the one save; nothing more sent
    report = result.report_path.read_text(encoding="utf-8")
    assert "State-changing actions" in report
    assert json.dumps("ACME-2026-0917")[1:-1] in report
