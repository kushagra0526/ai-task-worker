"""Command line entry point.

    python -m worker run "Find the latest invoice from Acme Cloud ..."
    python -m worker run --task-id invoice-to-erp --headed
    python -m worker tasks
    python -m worker console
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap

from .config import ROOT, Settings
from .events import Event

DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"


def _short(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def print_event(e: Event) -> None:
    who = f"{DIM}[{e.agent}#{e.step}]{RESET}"
    d = e.data
    if e.type == "run_started":
        print(f"{BOLD}Task:{RESET} {d['task']}\n{DIM}model={d['model']} approval={d['approval_mode']} "
              f"run_dir={d['run_dir']}{RESET}\n")
    elif e.type == "plan_updated":
        print(f"{who} {CYAN}plan{RESET}" + (f" {DIM}({_short(d['rationale'], 100)}){RESET}" if d.get("rationale") else ""))
        marks = {"done": "x", "in_progress": ">", "failed": "!", "skipped": "-", "pending": " "}
        for s in d["steps"]:
            print(f"      [{marks.get(s['status'], ' ')}] {s['title']}")
    elif e.type == "llm_response":
        if d.get("thinking"):
            print(f"{who} {DIM}thinking: {_short(d['thinking'], 220)}{RESET}")
        if d.get("text"):
            print(f"{who} {_short(d['text'], 300)}")
    elif e.type == "tool_call":
        tag = f"{d['risk']}/{d['decision']}"
        color = RED if d["risk"] == "external" else YELLOW if d["risk"] == "write" else DIM
        print(f"{who} -> {BOLD}{d['tool']}{RESET} {_short(json.dumps(d['args'], ensure_ascii=False), 140)} {color}[{tag}]{RESET}")
    elif e.type == "tool_result":
        first = _short(d["text"].splitlines()[0] if d["text"] else "", 150)
        print(f"      {RED + 'x ' if d['is_error'] else GREEN + 'ok '}{RESET}{first}")
    elif e.type == "memory_updated":
        print(f"{who} {CYAN}remember{RESET} {d['key']} = {d['value']} {DIM}({_short(d['source'], 80)}){RESET}")
    elif e.type == "guard_notice":
        print(f"{who} {YELLOW}harness: {_short(d['message'], 200)}{RESET}")
    elif e.type == "approval_decision":
        print(f"      {'approved' if d['approved'] else RED + 'declined' + RESET}")
    elif e.type == "terminal_call" and e.agent == "worker":
        print(f"\n{who} {BOLD}finish{RESET} status={d['args'].get('status')}: {_short(d['args'].get('summary', ''), 300)}")
    elif e.type == "verification_started":
        print(f"\n{BOLD}Independent verification...{RESET}")
    elif e.type == "verification_result":
        color = GREEN if d["verdict"] == "verified" else RED
        print(f"{color}{BOLD}verdict: {d['verdict']}{RESET} - {_short(d.get('summary', ''), 300)}")
        for c in d.get("checks", []):
            print(f"   {'PASS' if c['passed'] else RED + 'FAIL' + RESET} {_short(c['claim'], 90)} {DIM}| {_short(c['observed'], 90)}{RESET}")
    elif e.type == "repair_started":
        print(f"\n{YELLOW}Verification failed - worker is repairing (round {d['round']}){RESET}")
    elif e.type == "error":
        print(f"{RED}error: {d['message']}{RESET}")
    elif e.type == "run_finished":
        color = GREEN if d["status"] == "completed_verified" else YELLOW if d["status"].startswith("completed") else RED
        u = d["usage"]
        print(f"\n{color}{BOLD}{d['status'].upper()}{RESET}\n{textwrap.fill(d['summary'], 100)}")
        if d.get("result"):
            for k, v in d["result"].items():
                print(f"   {k}: {v}")
        print(f"{DIM}{u['llm_calls']} model calls, est. ${u['cost_usd']:.3f}{RESET}")
    elif e.type == "report_written":
        print(f"{DIM}report: {d['path']}{RESET}")


def load_tasks() -> list[dict]:
    return json.loads((ROOT / "tasks.json").read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="worker", description="Autonomous AI task worker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run a task")
    run_p.add_argument("task", nargs="?", help="natural-language task")
    run_p.add_argument("--task-id", help="run an example task from tasks.json")
    run_p.add_argument("--approval", default="guarded", choices=["guarded", "strict", "autonomous"])
    run_p.add_argument("--headed", action="store_true", help="show the browser window")
    run_p.add_argument("--max-steps", type=int, default=40)
    run_p.add_argument("--no-verify", action="store_true", help="skip independent verification")
    run_p.add_argument("--model", help="override WORKER_MODEL")

    sub.add_parser("tasks", help="list example tasks")
    con_p = sub.add_parser("console", help="start the web console")
    con_p.add_argument("--port", type=int, default=8800)

    args = parser.parse_args(argv)
    if args.cmd == "tasks":
        for t in load_tasks():
            print(f"{BOLD}{t['id']}{RESET}  {t['title']}\n    {t['task']}")
        return 0
    if args.cmd == "console":
        from console.server import serve

        serve(args.port)
        return 0

    task = args.task
    if args.task_id:
        task = next((t["task"] for t in load_tasks() if t["id"] == args.task_id), None)
        if task is None:
            parser.error(f"unknown task id {args.task_id!r}")
    if not task:
        parser.error("give a task or --task-id")

    from .orchestrator import run_task
    from .user import CLIUser

    settings = Settings(approval_mode=args.approval, headless=not args.headed, max_steps=args.max_steps,
                        verify=not args.no_verify)
    if args.model:
        settings.model = args.model
    result = run_task(task, settings, CLIUser(), listeners=[print_event])
    return 0 if result.status == "completed_verified" else 1
