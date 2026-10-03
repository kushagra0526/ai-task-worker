"""The observe-decide-act loop shared by the worker and the verifier.

The loop is deliberately thin about *what* to do - that is the model's job -
and strict about *how* things happen: every tool call is validated, risk-
classified and approved before it runs, its result is scrubbed of secrets and
recorded, and guards watch for the ways agents typically go wrong (looping,
error spirals, running out of budget, stopping without reporting).
"""

from __future__ import annotations

import base64
import json
import time
from collections import deque
from dataclasses import dataclass, field

from .context import build_view
from .llm import LLM, ContextTooLarge, LLMError, LLMResponse, estimate_cost
from .policy import ApprovalPolicy, Decision, Risk
from .runtime import RunState, ToolContext
from .tools import Tool, ToolError, ToolResult, validate_args

MAX_TEXT_NUDGES = 2
ERROR_STREAK_WARN = 3
ERROR_STREAK_ABORT = 7
REPEAT_WINDOW = 3


@dataclass
class LoopOutcome:
    terminal_args: dict | None  # arguments of the finish/submit_verdict call
    reason: str  # "terminal" | "budget" | "no_progress" | "error_spiral" | "refusal" | "llm_error"
    detail: str = ""


@dataclass
class Guard:
    """Detects unproductive patterns and produces corrective notices for the model."""

    recent: deque = field(default_factory=lambda: deque(maxlen=REPEAT_WINDOW))
    error_streak: int = 0

    def record(self, name: str, args: dict, result: ToolResult) -> list[str]:
        notices = []
        key = (name, json.dumps(args, sort_keys=True), result.text[:500])
        self.recent.append(key)
        if len(self.recent) == REPEAT_WINDOW and len(set(self.recent)) == 1:
            notices.append(f"You have made the same {name} call {REPEAT_WINDOW} times with the same result. It is not "
                           "working; re-examine the current state and try a different approach, or finish/ask.")
            self.recent.clear()
        self.error_streak = self.error_streak + 1 if result.is_error else 0
        if self.error_streak == ERROR_STREAK_WARN:
            notices.append(f"{ERROR_STREAK_WARN} tool calls in a row have failed. Step back: observe the current state, "
                           "reconsider the plan (update_plan), and change approach rather than retrying the same thing.")
        return notices


class AgentLoop:
    def __init__(self, *, name: str, llm: LLM, system: str, tools: list[Tool], policy: ApprovalPolicy,
                 run: RunState, max_steps: int, terminal: str):
        self.name = name
        self.llm = llm
        self.system = system
        self.tools = {t.name: t for t in tools}
        self.tool_defs = [t.definition() for t in tools]
        self.policy = policy
        self.run = run
        self.max_steps = max_steps
        self.terminal = terminal
        self.ctx = ToolContext(run=run, agent=name)
        self.messages: list[dict] = []
        self.steps_used = 0
        self.guard = Guard()

    def emit(self, type: str, data: dict) -> None:
        self.run.bus.emit(type, data, agent=self.name, step=self.steps_used)

    # ------------------------------------------------------------------ main loop

    def run_until_terminal(self, user_content: str) -> LoopOutcome:
        """Append a user message and run until the terminal tool is called or a stop condition hits.
        Can be called again later on the same loop to continue the conversation."""
        self.messages.append({"role": "user", "content": user_content})
        nudges = 0
        budget_warned = False
        while self.steps_used < self.max_steps:
            self.steps_used += 1
            self.ctx.step = self.steps_used
            try:
                resp = self._call_model()
            except LLMError as e:
                self.emit("error", {"message": str(e)})
                return LoopOutcome(None, "llm_error", str(e))
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "refusal":
                return LoopOutcome(None, "refusal", "The model declined to continue this task.")

            if not resp.tool_calls:
                if resp.stop_reason == "max_tokens":
                    self.messages.append({"role": "user", "content": "[harness] Your response was cut off. Continue, more concisely."})
                    continue
                nudges += 1
                if nudges > MAX_TEXT_NUDGES:
                    return LoopOutcome(None, "no_progress", resp.text)
                self.messages.append({"role": "user", "content": (
                    f"[harness] No tool was called. Continue working with your tools, or call `{self.terminal}` "
                    "if you are done or cannot proceed.")})
                continue

            results, notices, terminal_args = self._execute_calls(resp)
            if self.guard.error_streak >= ERROR_STREAK_ABORT:
                return LoopOutcome(None, "error_spiral", f"{ERROR_STREAK_ABORT} consecutive tool failures")
            remaining = self.max_steps - self.steps_used
            if terminal_args is None and remaining <= 3 and not budget_warned:
                budget_warned = True
                notices.append(f"Only {remaining} steps remain in your budget. Wrap up now: confirm what you can and "
                               f"call `{self.terminal}` with an honest status.")
            for notice in notices:
                self.emit("guard_notice", {"message": notice})
                results.append({"type": "text", "text": f"[harness] {notice}"})
            self.messages.append({"role": "user", "content": results})
            if terminal_args is not None:
                return LoopOutcome(terminal_args, "terminal")
        return LoopOutcome(None, "budget", f"step budget of {self.max_steps} exhausted")

    def _call_model(self) -> LLMResponse:
        started = time.time()
        context: dict = {}
        for attempt in range(3):
            budget = self.llm.message_budget(self.system, self.tool_defs)
            view = self.messages
            if budget is not None:  # token-budgeted provider: send a compacted view of the transcript
                view, context = build_view(self.messages, budget, self.llm.count_tokens,
                                           plan=self.run.plan if self.name == "worker" else None, memory=self.run.memory)
            try:
                resp = self.llm.respond(self.system, view, self.tool_defs)
                break
            except ContextTooLarge:
                if attempt == 2:
                    raise
                self.llm.shrink()
        cost = estimate_cost(resp.model or self.llm.model, resp.usage)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            self.run.usage[key] += resp.usage.get(key, 0)
        self.run.usage["cost_usd"] += cost
        self.run.usage["llm_calls"] += 1
        self.emit("llm_response", {
            "text": resp.text, "thinking": resp.thinking, "stop_reason": resp.stop_reason,
            "tool_calls": [c.name for c in resp.tool_calls], "usage": resp.usage,
            "cost_usd": round(cost, 5), "latency_s": round(time.time() - started, 2), "model": resp.model,
            "context": context,
        })
        return resp

    # ------------------------------------------------------------------ tool execution

    def _execute_calls(self, resp: LLMResponse) -> tuple[list[dict], list[str], dict | None]:
        results, notices, terminal_args = [], [], None
        # Run ordinary tools first so a finish call batched with actions sees their effects.
        for call in sorted(resp.tool_calls, key=lambda c: c.name == self.terminal):
            if call.name == self.terminal:
                errors = validate_args(self.tools[call.name].input_schema, call.input)
                if errors:
                    result = ToolResult("Invalid call: " + "; ".join(errors), is_error=True)
                else:
                    terminal_args = call.input
                    self.emit("terminal_call", {"tool": call.name, "args": call.input})
                    result = ToolResult("Received.")
            else:
                result = self._execute_one(call.name, call.input)
                notices += self.guard.record(call.name, call.input, result)
            results.append(self._to_block(call.id, result))
        return results, notices, terminal_args

    def _execute_one(self, name: str, args: dict) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool '{name}'. Available: {', '.join(self.tools)}", is_error=True)
        errors = validate_args(tool.input_schema, args)
        if errors:
            return ToolResult("Invalid arguments: " + "; ".join(errors), is_error=True)

        try:
            risk = tool.classify(args, self.ctx)
        except Exception as e:  # classification must never crash the run
            return ToolResult(f"Could not inspect the target of this action: {e}", is_error=True)
        decision = self.policy.decide(risk)
        self.emit("tool_call", {"tool": name, "args": args, "risk": risk.value, "decision": decision.value})

        if decision is Decision.DENY:
            result = ToolResult(f"Blocked: this action is classified '{risk.value}' and is not permitted in this mode "
                                f"({self.policy.mode}). Choose another way to achieve the goal.", is_error=True)
            self.emit("tool_result", {"tool": name, "is_error": True, "text": result.text, "evidence": []})
            return result

        # Duplicate protection: compare this state change with what the ledger says already happened.
        fingerprint, origin, duplicate_note = None, "", ""
        if risk is not Risk.READ:
            try:
                fingerprint, origin = tool.fingerprint(args, self.ctx), tool.origin(args, self.ctx)
            except Exception:
                fingerprint = None
            prior = next((e for e in reversed(self.run.ledger) if fingerprint and e["fingerprint"] == fingerprint), None)
            if prior and prior["outcome"] == "unknown" and not prior["checked"]:
                return self._refuse(name, prior, (
                    f"Blocked by the harness: an identical submission at step {prior['step']} had an unknown outcome "
                    f"({prior['detail']}). Repeating it blindly could create a duplicate. First check the current state "
                    "somewhere other than the form itself (e.g. the list of records, or a GET on the API); if it really "
                    "was not applied, you may then submit again."))
            if prior and prior["outcome"] == "applied":
                if self.policy.mode not in ("guarded", "strict"):
                    return self._refuse(name, prior, f"Blocked by the harness: an identical submission already succeeded "
                                                     f"at step {prior['step']}. Repeating it would create a duplicate.")
                decision = Decision.ASK
                duplicate_note = (f"POSSIBLE DUPLICATE: an identical submission already succeeded at step "
                                  f"{prior['step']}.\n")

        if decision is Decision.ASK:
            description = duplicate_note + tool.describe_action(args, self.ctx)
            self.emit("approval_requested", {"tool": name, "risk": risk.value, "action": description})
            approved = self.run.user.approve(description, risk.value, f"'{risk.value}' actions need human approval")
            self.emit("approval_decision", {"tool": name, "approved": approved})
            if not approved:
                result = ToolResult("The user did NOT approve this action. Do not retry it. Continue another way if "
                                    "one exists, otherwise finish with status 'blocked' and explain.", is_error=True)
                self.emit("tool_result", {"tool": name, "is_error": True, "text": result.text, "evidence": []})
                return result

        started = time.time()
        try:
            result = tool.run(args, self.ctx)
        except ToolError as e:
            result = ToolResult(str(e), is_error=True)
        except Exception as e:  # unexpected tool failure: report it to the model, keep the run alive
            first_line = (str(e).strip().splitlines() or [""])[0]
            result = ToolResult(f"{name} failed unexpectedly: {type(e).__name__}: {first_line[:300]}", is_error=True)
        result.text = self.run.vault.mask(result.text)
        if risk is Risk.READ:
            self._note_observation(name, args, result)
        elif fingerprint:
            self._record_write(name, fingerprint, origin, result)
        self.emit("tool_result", {
            "tool": name, "is_error": result.is_error, "text": result.text[:4000],
            "evidence": result.evidence, "duration_s": round(time.time() - started, 2), "meta": result.meta,
        })
        return result

    def _refuse(self, name: str, prior: dict, text: str) -> ToolResult:
        self.emit("duplicate_blocked", {"tool": name, "prior_step": prior["step"], "outcome": prior["outcome"]})
        self.emit("tool_result", {"tool": name, "is_error": True, "text": text, "evidence": []})
        return ToolResult(text, is_error=True)

    def _record_write(self, tool: str, fingerprint: str, origin: str, result: ToolResult) -> None:
        status = result.meta.get("status")
        if result.meta.get("applied"):
            outcome, detail = "applied", "completed locally"
        elif isinstance(status, int):
            outcome = "applied" if status < 400 else "rejected" if status < 500 else "unknown"
            detail = f"HTTP {status}"
        elif result.meta.get("attempted") or not result.is_error:
            outcome, detail = "unknown", "no response observed"
        else:
            return  # failed before anything was sent
        entry = {"step": self.steps_used, "agent": self.name, "tool": tool, "fingerprint": self.run.vault.mask(fingerprint),
                 "origin": origin, "outcome": outcome, "detail": detail, "checked": False}
        self.run.ledger.append(entry)
        self.emit("write_recorded", entry)

    def _note_observation(self, name: str, args: dict, result: ToolResult) -> None:
        """A successful page load or API read somewhere other than where a submission came from counts as having
        checked the state after an unknown outcome. Re-reading the form or the error page does not."""
        status = result.meta.get("status")
        if result.is_error or not isinstance(status, int) or status >= 400:
            return
        location = f"GET {args.get('url', '')}" if name == "http_request" else result.meta.get("url")
        for entry in self.run.ledger:
            if entry["outcome"] == "unknown" and not entry["checked"] and location and location != entry["origin"]:
                entry["checked"] = True

    @staticmethod
    def _to_block(tool_use_id: str, result: ToolResult) -> dict:
        content: list[dict] = [{"type": "text", "text": result.text or "(no output)"}]
        for png in result.images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(png).decode()}})
        block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if result.is_error:
            block["is_error"] = True
        return block
