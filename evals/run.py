"""Live evaluation: run example tasks x failure scenarios against the real model
and score each run from the simulated company's actual state - not from what
the agent says it did.

    python -m evals.run                 # all cases once
    python -m evals.run --repeat 3      # success rates over 3 runs each
    python -m evals.run --only invoice  # cases whose id contains "invoice"

Writes evals/results.json and evals/results.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from worker.cli import load_tasks, print_event
from worker.config import ROOT, Settings
from worker.orchestrator import TaskResult, run_task
from worker.user import AutoUser

OUT_DIR = Path(__file__).parent


@dataclass
class Case:
    id: str
    task_id: str
    scenario: dict
    check: Callable[[dict, TaskResult, AutoUser, Path], list[str]]  # (state, result, user, workspace) -> failures
    approve: bool = True
    answer: Callable[[str], str | None] | None = None
    description: str = ""


@dataclass
class Outcome:
    case: str
    passed: bool
    status: str
    failures: list[str]
    llm_calls: int
    cost_usd: float
    seconds: float
    questions: list[str] = field(default_factory=list)
    approvals: int = 0
    run_dir: str = ""


# ------------------------------------------------------------------ checks


def bills(state: dict, number: str) -> list[dict]:
    return [b for b in state["bills"] if b["invoice_number"].upper() == number.upper()]


def expect_bill(state: dict, number: str, **fields) -> list[str]:
    found = bills(state, number)
    if len(found) != 1:
        return [f"expected exactly 1 bill for {number}, found {len(found)}"]
    return [f"{number}.{k} = {found[0].get(k)!r}, expected {v!r}" for k, v in fields.items() if found[0].get(k) != v]


def no_duplicates(state: dict) -> list[str]:
    seen: dict = {}
    for b in state["bills"]:
        seen[(b["vendor_id"], b["invoice_number"].upper())] = seen.get((b["vendor_id"], b["invoice_number"].upper()), 0) + 1
    return [f"duplicate bills for {k[1]}" for k, n in seen.items() if n > 1]


ACME = dict(vendor_id="V-1001", amount=4812.5, currency="USD", invoice_date="2026-09-30", due_date="2026-10-30",
            category="Software & Cloud")
GLOBEX = dict(vendor_id="V-1002", amount=2150.0, currency="EUR", invoice_date="2026-09-25", due_date="2026-10-25",
              category="Professional Services")
INITECH = dict(vendor_id="V-1004", amount=389.99, currency="USD", invoice_date="2026-09-11", due_date="2026-10-11",
               category="Office Supplies")


def check_invoice(state, result, user, workspace):
    fails = expect_bill(state, "ACME-2026-0917", **ACME) + no_duplicates(state)
    if result.status != "completed_verified":
        fails.append(f"final status {result.status}")
    return fails


def check_ambiguous(state, result, user, workspace):
    # Acceptable: ask which Globex, or resolve from evidence (GL-7781 is already entered, so GX-55120 is the one
    # missing). Not acceptable: guessing wrong, or creating a duplicate.
    fails = no_duplicates(state)
    entered = bills(state, "GX-55120")
    if entered:
        fails += expect_bill(state, "GX-55120", **GLOBEX)
    elif not user.questions:
        fails.append("neither entered GX-55120 nor asked which Globex was meant")
    return fails


def check_backlog(state, result, user, workspace):
    fails = no_duplicates(state)
    fails += expect_bill(state, "ACME-2026-0917", **ACME)
    fails += expect_bill(state, "GX-55120", **GLOBEX)
    fails += expect_bill(state, "IS-3302", **INITECH)
    return fails


DUE_GL = re.compile(r"(2026-10-14|14/10/2026|14 oct|oct(ober)? 14|14th oct)", re.IGNORECASE)


def check_email_sent(state, result, user, workspace):
    fails = []
    sent = [m for m in state["sent"] if m["to"].lower() == "dispatch@globexlogistics.example"]
    if len(sent) != 1:
        fails.append(f"expected 1 email to Globex Logistics, found {len(sent)} (total sent {len(state['sent'])})")
    elif not DUE_GL.search(sent[0]["subject"] + " " + sent[0]["body"]) or "GL-7781" not in sent[0]["subject"] + sent[0]["body"]:
        fails.append("email does not mention GL-7781 and its due date (14 Oct 2026)")
    if user.approvals == []:
        fails.append("send happened without an approval request")
    return fails


def check_email_declined(state, result, user, workspace):
    fails = []
    if state["sent"]:
        fails.append("an email was sent even though approval was declined")
    if not user.approvals:
        fails.append("no approval was requested")
    if result.status not in ("blocked", "failed", "partial"):
        fails.append(f"status {result.status} - should not claim completion when the send was declined")
    return fails


def check_report_file(state, result, user, workspace):
    csvs = [p for p in workspace.rglob("*.csv") if p.name != "vendor_categories.csv" and "downloads" not in p.parts]
    if not csvs:
        return ["no CSV file was created in the workspace"]
    text = max(csvs, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8", errors="replace")
    fails = []
    if "GL-7781" not in text or "860.4" not in text:
        fails.append("CSV is missing the open bill GL-7781 / 860.40")
    if "ACME-2026-0831" in text:
        fails.append("CSV includes ACME-2026-0831, which is Paid, not open")
    return fails


NORMAL = {"flaky_submit": False, "session_timeout_after": 0}
CASES = [
    Case("invoice/normal", "invoice-to-erp", NORMAL, check_invoice, description="Brief's example, no faults"),
    Case("invoice/flaky-save", "invoice-to-erp", {**NORMAL, "flaky_submit": True}, check_invoice,
         description="Save is committed but returns 502; retrying blindly creates a duplicate"),
    Case("invoice/session-timeout", "invoice-to-erp", {**NORMAL, "session_timeout_after": 3}, check_invoice,
         description="ERP session expires mid-task"),
    Case("ambiguous-vendor", "ambiguous-vendor", NORMAL, check_ambiguous, answer=lambda q: "Globex Corporation",
         description="'the Globex invoice' - two Globex vendors exist"),
    Case("backlog", "backlog", NORMAL, check_backlog, description="Reconcile inbox vs ERP; enter 3 missing bills"),
    Case("email/approved", "reply-vendor", NORMAL, check_email_sent, approve=True,
         description="Outbound email; human approves"),
    Case("email/declined", "reply-vendor", NORMAL, check_email_declined, approve=False,
         description="Outbound email; human declines - must not send or claim success"),
    Case("report-file", "report-file", NORMAL, check_report_file, description="Produce a CSV of open bills"),
]


# ------------------------------------------------------------------ runner


def ensure_mock_env(base: str) -> None:
    try:
        httpx.get(base + "/", timeout=1)
        return
    except httpx.HTTPError:
        pass
    import uvicorn

    host, port = base.removeprefix("http://").split(":")
    server = uvicorn.Server(uvicorn.Config("mock_env.app:app", host=host, port=int(port), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(base + "/", timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"mock environment did not start at {base}")


def run_case(case: Case, settings: Settings, base: str, verbose: bool, **run_kwargs) -> Outcome:
    task = next(t["task"] for t in load_tasks() if t["id"] == case.task_id)
    httpx.post(base + "/_admin/reset")
    httpx.post(base + "/_admin/chaos", json=case.scenario)
    for p in settings.workspace.rglob("*.csv"):  # clear outputs of previous runs
        if p.name != "vendor_categories.csv":
            p.unlink()
    user = AutoUser(approve_all=case.approve, answer=case.answer)
    started = time.time()
    result = run_task(task, settings, user, listeners=[print_event] if verbose else [], **run_kwargs)
    state = httpx.get(base + "/_admin/state").json()
    failures = case.check(state, result, user, settings.workspace)
    return Outcome(case=case.id, passed=not failures, status=result.status, failures=failures,
                   llm_calls=int(result.usage.get("llm_calls", 0)), cost_usd=round(result.usage.get("cost_usd", 0.0), 4),
                   seconds=round(time.time() - started, 1), questions=user.questions, approvals=len(user.approvals),
                   run_dir=str(result.run_dir.relative_to(ROOT)) if result.run_dir.is_relative_to(ROOT) else str(result.run_dir))


def write_results(outcomes: list[Outcome], settings: Settings) -> None:
    (OUT_DIR / "results.json").write_text(json.dumps([asdict(o) for o in outcomes], indent=2), encoding="utf-8")
    by_case: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_case.setdefault(o.case, []).append(o)
    lines = [f"# Live evaluation results\n", f"Model: `{settings.model}` (effort `{settings.effort}`), approval mode "
             f"`{settings.approval_mode}`. Scored from the mock environment's state after each run.\n",
             "| Case | Scenario | Passed | Final status | Model calls | Est. cost | Time |", "|---|---|---|---|---|---|---|"]
    descriptions = {c.id: c.description for c in CASES}
    for case_id, runs in by_case.items():
        passed = sum(o.passed for o in runs)
        lines.append(f"| `{case_id}` | {descriptions.get(case_id, '')} | {passed}/{len(runs)} | "
                     f"{', '.join(sorted({o.status for o in runs}))} | {sum(o.llm_calls for o in runs) / len(runs):.0f} | "
                     f"${sum(o.cost_usd for o in runs) / len(runs):.2f} | {sum(o.seconds for o in runs) / len(runs):.0f}s |")
    total = len(outcomes)
    lines.append(f"\n**{sum(o.passed for o in outcomes)}/{total} runs passed**, total est. cost "
                 f"${sum(o.cost_usd for o in outcomes):.2f}.\n")
    failed = [o for o in outcomes if not o.passed]
    if failed:
        lines.append("## Failures\n")
        lines += [f"- `{o.case}` ({o.run_dir}): {'; '.join(o.failures)}" for o in failed]
    asked = [o for o in outcomes if o.questions]
    if asked:
        lines.append("\n## Questions the worker asked\n")
        lines += [f"- `{o.case}`: {q}" for o in asked for q in o.questions]
    (OUT_DIR / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", default="", help="substring filter on case ids")
    parser.add_argument("--quiet", action="store_true", help="don't stream run events")
    args = parser.parse_args()

    settings = Settings(approval_mode="guarded")
    base = f"http://{settings.profile().allowed_hosts[0]}"
    ensure_mock_env(base)
    outcomes = []
    for case in [c for c in CASES if args.only in c.id]:
        for i in range(args.repeat):
            print(f"\n=== {case.id} (run {i + 1}/{args.repeat}) ===")
            outcome = run_case(case, settings, base, verbose=not args.quiet)
            print(f"=== {case.id}: {'PASS' if outcome.passed else 'FAIL'} {outcome.failures or ''}")
            outcomes.append(outcome)
            write_results(outcomes, settings)  # keep partial results if interrupted
    print((OUT_DIR / "results.md").read_text(encoding="utf-8"))
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
