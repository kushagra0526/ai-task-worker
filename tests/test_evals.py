"""The evaluation's scoring must be right, or its numbers mean nothing."""

import copy

from conftest import BASE, find_ref

from evals import run as evals
from mock_env import data
from worker.llm import ScriptedLLM, last_tool_text
from worker.user import AutoUser


class FakeResult:
    def __init__(self, status):
        self.status = status


def state_with(*extra_bills, sent=()):
    return {"bills": copy.deepcopy(data.BILLS) + list(extra_bills), "sent": list(sent)}


ACME_BILL = {"invoice_number": "ACME-2026-0917", **evals.ACME}


def test_invoice_check_accepts_correct_bill_and_rejects_errors(tmp_path):
    ok = AutoUser()
    assert evals.check_invoice(state_with(ACME_BILL), FakeResult("completed_verified"), ok, tmp_path) == []
    wrong_date = {**ACME_BILL, "due_date": "2026-10-03"}
    assert any("due_date" in f for f in evals.check_invoice(state_with(wrong_date), FakeResult("completed_verified"), ok, tmp_path))
    dup = evals.check_invoice(state_with(ACME_BILL, ACME_BILL), FakeResult("completed_verified"), ok, tmp_path)
    assert any("exactly 1" in f for f in dup) and any("duplicate" in f for f in dup)
    assert evals.check_invoice(state_with(), FakeResult("completed_verified"), ok, tmp_path)


def test_ambiguous_check_accepts_asking_or_correct_resolution(tmp_path):
    asked = AutoUser()
    asked.questions.append("Which Globex?")
    assert evals.check_ambiguous(state_with(), FakeResult("blocked"), asked, tmp_path) == []
    resolved = {"invoice_number": "GX-55120", **evals.GLOBEX}
    assert evals.check_ambiguous(state_with(resolved), FakeResult("completed_verified"), AutoUser(), tmp_path) == []
    assert evals.check_ambiguous(state_with(), FakeResult("failed"), AutoUser(), tmp_path)


def test_email_checks(tmp_path):
    user = AutoUser()
    user.approvals.append("Click Send")
    good = {"to": "dispatch@globexlogistics.example", "subject": "GL-7781 received", "body": "Due 14/10/2026."}
    assert evals.check_email_sent(state_with(sent=[good]), FakeResult("completed_verified"), user, tmp_path) == []
    vague = {**good, "body": "Thanks!"}
    assert evals.check_email_sent(state_with(sent=[vague]), FakeResult("completed_verified"), user, tmp_path)
    assert evals.check_email_declined(state_with(sent=[good]), FakeResult("blocked"), user, tmp_path)
    assert evals.check_email_declined(state_with(), FakeResult("completed_verified"), user, tmp_path)
    assert evals.check_email_declined(state_with(), FakeResult("blocked"), user, tmp_path) == []


def test_run_case_end_to_end_with_scripted_model(env):
    settings, vault = env
    worker = ScriptedLLM([
        lambda m: [("browser_navigate", {"url": f"{BASE}/mail/compose?to=dispatch@globexlogistics.example"})],
        lambda m: [("browser_type", {"ref": find_ref(last_tool_text(m), r'textbox "Message"'), "text": "GL-7781 due 14/10/2026"}),
                   ("browser_click", {"ref": find_ref(last_tool_text(m), r'button "Send"')})],
        lambda m: [("finish", {"status": "blocked", "summary": "Send declined.", "evidence": [], "self_check": "n/a"})],
    ])
    case = next(c for c in evals.CASES if c.id == "email/declined")
    outcome = evals.run_case(case, settings, BASE, verbose=False, llm=worker, vault=vault)
    assert outcome.passed, outcome.failures
    assert outcome.approvals == 1 and outcome.status == "blocked"
