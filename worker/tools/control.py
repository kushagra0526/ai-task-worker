"""Tools that act on the run itself: planning, memory, the human, and completion."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .base import Tool, ToolError, ToolResult

if TYPE_CHECKING:
    from ..runtime import ToolContext

PLAN_STATUSES = ["pending", "in_progress", "done", "failed", "skipped"]


class UpdatePlan(Tool):
    name = "update_plan"
    description = ("Create or revise your plan: the full ordered list of steps with their status. Call it at the start, "
                   "whenever you learn something that changes the approach, and as steps complete.")
    input_schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": PLAN_STATUSES},
                    },
                    "required": ["title", "status"],
                },
            },
            "rationale": {"type": "string", "description": "Why the plan looks like this / what changed"},
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        steps = [{"title": str(s.get("title", "")), "status": s.get("status", "pending")} for s in args["steps"]]
        bad = [s["status"] for s in steps if s["status"] not in PLAN_STATUSES]
        if bad:
            raise ToolError(f"Invalid status values: {bad}. Use one of {PLAN_STATUSES}.")
        ctx.run.plan = steps
        ctx.emit("plan_updated", {"steps": steps, "rationale": args.get("rationale", "")})
        return ToolResult("Plan recorded.")


class Remember(Tool):
    name = "remember"
    description = ("Record facts that matter for the task (extracted values, IDs you looked up or created, decisions), "
                   "each with where it came from. Record everything you extracted from a source in one call. Working "
                   "memory stays visible to you for the whole task, and is shown to the verifier and in the report.")
    input_schema = {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Short snake_case name, e.g. invoice_amount"},
                        "value": {"type": "string"},
                        "source": {"type": "string", "description": "e.g. 'ACME-2026-0917.pdf, Total due line'"},
                    },
                    "required": ["key", "value", "source"],
                },
            },
        },
        "required": ["facts"],
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        if not args["facts"]:
            raise ToolError("No facts given.")
        for fact in args["facts"]:
            if not all(isinstance(fact.get(k), str) and fact.get(k) for k in ("key", "value", "source")):
                raise ToolError(f"Each fact needs non-empty string key, value and source; got {fact!r}.")
        for fact in args["facts"]:
            previous = ctx.run.memory.get(fact["key"])
            ctx.run.memory[fact["key"]] = {"value": fact["value"], "source": fact["source"], "step": ctx.step}
            ctx.emit("memory_updated", {"key": fact["key"], "value": fact["value"], "source": fact["source"],
                                        "previous": previous["value"] if previous else None})
        return ToolResult(f"Remembered. Working memory now:\n{format_memory(ctx.run.memory)}")


class AskUser(Tool):
    name = "ask_user"
    description = ("Ask the human a question and wait for the answer. Use it when the request is ambiguous in a way that "
                   "changes the outcome (e.g. two plausible matches), or when you need a decision that is not yours to "
                   "make. Do not use it for things you can find out yourself.")
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers, if any"},
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        ctx.emit("user_question", {"question": args["question"], "options": args.get("options", [])})
        answer = ctx.run.user.ask(args["question"], args.get("options"))
        ctx.emit("user_answer", {"answer": answer})
        if answer is None:
            return ToolResult("No user is available to answer right now. Proceed only if one interpretation is clearly "
                              "safest; otherwise finish with status 'blocked' and explain what you need.")
        return ToolResult(f"User answered: {answer}")


class Finish(Tool):
    """Terminal tool for the worker. Handled by the agent loop, never executed directly."""

    name = "finish"
    description = ("End the task. Call this once you have checked the outcome yourself. Be honest: use 'success' only "
                   "if the user's goal is actually achieved and you observed proof of it. An independent verifier will "
                   "re-check your claims against the live systems.")
    input_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["success", "partial", "failed", "blocked"]},
            "summary": {"type": "string", "description": "2-4 sentence report for the user: what was done and the outcome"},
            "result": {"type": "object", "description": "Key output values, e.g. {\"amount\": \"USD 4812.50\", \"bill_id\": \"AP-1043\"}"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"claim": {"type": "string"}, "source": {"type": "string"}},
                    "required": ["claim", "source"],
                },
                "description": "Each claim you make and where you observed it (URL, file, API response)",
            },
            "self_check": {"type": "string", "description": "How you confirmed the outcome before finishing"},
        },
        "required": ["status", "summary", "evidence", "self_check"],
        "additionalProperties": False,
    }


class SubmitVerdict(Tool):
    """Terminal tool for the verifier."""

    name = "submit_verdict"
    description = "Submit your verification verdict. Call exactly once, after checking each claim against live state."
    input_schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["verified", "not_verified", "inconclusive"]},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "observed": {"type": "string", "description": "What you actually saw, and where"},
                        "passed": {"type": "boolean"},
                    },
                    "required": ["claim", "observed", "passed"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["verdict", "checks", "summary"],
        "additionalProperties": False,
    }


def format_memory(memory: dict) -> str:
    if not memory:
        return "(none recorded)"
    return "\n".join(f"- {k} = {v['value']}  (source: {v['source']})" for k, v in memory.items())


def format_finish(args: dict) -> str:
    return json.dumps(args, indent=2, ensure_ascii=False)
