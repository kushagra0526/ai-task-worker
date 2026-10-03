"""Runs a task end to end: worker loop -> independent verification -> (repair) -> report.

Verification is a separate agent with a fresh context, a read-only policy and
an adversarial brief. The worker's own "I checked it" is necessary but not
sufficient; a second observer that didn't do the work catches the classic
failure where an agent convinces itself it succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import prompts
from .agent import AgentLoop, LoopOutcome
from .config import Settings
from .events import EventBus, new_run_dir
from .llm import LLM, make_llm
from .policy import ApprovalPolicy
from .report import write_report
from .runtime import RunState
from .secrets import SecretVault
from .tools import verifier_tools, worker_tools
from .tools.control import format_finish, format_memory
from .user import UserChannel


@dataclass
class TaskResult:
    status: str  # completed_verified | completed_unverified | partial | blocked | failed
    summary: str
    run_dir: Path
    finish: dict | None = None
    verification: list[dict] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    ledger: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    report_path: Path | None = None


def run_task(
    task: str,
    settings: Settings,
    user: UserChannel,
    *,
    llm: LLM | None = None,
    verifier_llm: LLM | None = None,
    vault: SecretVault | None = None,
    listeners: list = (),
    today: str | None = None,
) -> TaskResult:
    profile = settings.profile()
    settings.workspace.mkdir(parents=True, exist_ok=True)
    run_dir = new_run_dir(settings.runs_dir, task)
    bus = EventBus(run_dir)
    for listener in listeners:
        bus.subscribe(listener)
    run = RunState(settings=settings, profile=profile, bus=bus, vault=vault or SecretVault(profile.secrets),
                   user=user, run_dir=run_dir)
    if llm is None:
        llm = make_llm(settings, "worker")
        verifier_llm = verifier_llm or make_llm(settings, "verifier")
    verifier_llm = verifier_llm or llm
    for agent_name, model in (("worker", llm), ("verifier", verifier_llm)):
        if hasattr(model, "notify"):  # surface rate-limit waits and model switches in the trace
            model.notify = lambda type, data, agent=agent_name: bus.emit(type, data, agent=agent)
    today = today or date.today().isoformat()
    environment = profile.describe()

    bus.emit("run_started", {
        "task": task, "model": llm.model, "verifier_model": verifier_llm.model, "provider": settings.provider,
        "approval_mode": settings.approval_mode,
        "run_dir": str(run_dir), "secrets_available": run.vault.available,
    })
    worker = AgentLoop(
        name="worker", llm=llm, system=prompts.WORKER_SYSTEM.format(environment=environment),
        tools=_usable(worker_tools(), llm), policy=ApprovalPolicy(settings.approval_mode), run=run,
        max_steps=settings.max_steps, terminal="finish",
    )

    verifications: list[dict] = []
    finish: dict | None = None
    try:
        outcome = worker.run_until_terminal(prompts.worker_kickoff(task, today))
        rounds = 0
        while True:
            if outcome.terminal_args is None:
                status, summary = _abnormal_end(outcome)
                break
            finish = outcome.terminal_args
            if finish["status"] != "success":
                status, summary = finish["status"], finish["summary"]
                break
            if not settings.verify:
                status, summary = "completed_unverified", finish["summary"]
                break
            rounds += 1
            verdict = _verify(task, finish, run, verifier_llm, environment, today, settings)
            verifications.append(verdict)
            if verdict["verdict"] == "verified":
                status, summary = "completed_verified", finish["summary"]
                break
            if verdict["verdict"] == "inconclusive" or rounds >= settings.max_verification_rounds:
                status = "completed_unverified"
                summary = f"{finish['summary']}\n\nVerification: {verdict['verdict']} - {verdict.get('summary', '')}"
                break
            bus.emit("repair_started", {"round": rounds, "reason": verdict.get("summary", "")})
            outcome = worker.run_until_terminal(prompts.verification_feedback(verdict))
    except Exception as e:  # harness bug or environment crash: still produce a report
        bus.emit("error", {"message": f"{type(e).__name__}: {e}"})
        status, summary = "failed", f"The run stopped because of an internal error: {type(e).__name__}: {e}"
    finally:
        run.close()

    result = TaskResult(status=status, summary=summary, run_dir=run_dir, finish=finish, verification=verifications,
                        memory=run.memory, ledger=run.ledger.entries, usage=dict(run.usage))
    bus.emit("run_finished", {"status": status, "summary": summary, "usage": run.usage,
                              "result": (finish or {}).get("result")})
    result.report_path = write_report(run_dir, task, result, bus.events, run.plan)
    bus.emit("report_written", {"path": str(result.report_path)})
    bus.close()
    return result


def _usable(tools: list, llm: LLM) -> list:
    """Drop tools the model cannot use (screenshots are pointless for a text-only model)."""
    return [t for t in tools if llm.supports_images or t.name != "browser_screenshot"]


def _abnormal_end(outcome: LoopOutcome) -> tuple[str, str]:
    messages = {
        "budget": "I ran out of my step budget before finishing.",
        "no_progress": "I stopped without completing the task.",
        "error_spiral": "I stopped after repeated tool failures.",
        "refusal": "The model declined to continue this task.",
        "llm_error": "The model API call failed.",
    }
    text = messages.get(outcome.reason, "The run ended unexpectedly.")
    if outcome.detail:
        text += f" Detail: {outcome.detail}"
    return "failed", text


def _verify(task: str, finish: dict, run: RunState, llm: LLM, environment: str, today: str,
            settings: Settings) -> dict:
    run.bus.emit("verification_started", {"claim": finish.get("summary", "")}, agent="verifier")
    verifier = AgentLoop(
        name="verifier", llm=llm, system=prompts.VERIFIER_SYSTEM.format(environment=environment),
        tools=_usable(verifier_tools(), llm), policy=ApprovalPolicy("read_only"), run=run,
        max_steps=settings.verifier_max_steps, terminal="submit_verdict",
    )
    outcome = verifier.run_until_terminal(
        prompts.verifier_kickoff(task, format_finish(finish), format_memory(run.memory), today,
                                 prompts.format_ledger(run.ledger.entries)))
    verdict = outcome.terminal_args or {
        "verdict": "inconclusive", "checks": [],
        "summary": f"Verifier did not reach a verdict ({outcome.reason}: {outcome.detail})",
    }
    run.bus.emit("verification_result", verdict, agent="verifier")
    return verdict

