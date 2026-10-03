"""How the worker reaches a human: clarification questions and action approvals.

The agent loop only knows the `UserChannel` interface; the CLI, the web
console and tests each provide their own implementation.
"""

from __future__ import annotations

import queue
import threading
import uuid
from typing import Callable, Protocol


class UserChannel(Protocol):
    def ask(self, question: str, options: list[str] | None) -> str | None:
        """Return the user's answer, or None if nobody is available to answer."""

    def approve(self, action: str, risk: str, reason: str) -> bool:
        """Return True only on an explicit yes."""


class CLIUser:
    def ask(self, question: str, options: list[str] | None) -> str | None:
        print(f"\n\033[1;33m? Worker asks:\033[0m {question}")
        if options:
            for i, opt in enumerate(options, 1):
                print(f"   {i}. {opt}")
        answer = input("  your answer> ").strip()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer or None

    def approve(self, action: str, risk: str, reason: str) -> bool:
        print(f"\n\033[1;31m! Approval needed ({risk}):\033[0m {action}")
        if reason:
            print(f"  why: {reason}")
        return input("  approve? [y/N]> ").strip().lower() in ("y", "yes")


class AutoUser:
    """Non-interactive channel for tests and unattended runs."""

    def __init__(self, approve_all: bool = False, answer: Callable[[str], str | None] | None = None):
        self.approve_all = approve_all
        self.answer = answer
        self.questions: list[str] = []
        self.approvals: list[str] = []

    def ask(self, question: str, options: list[str] | None) -> str | None:
        self.questions.append(question)
        return self.answer(question) if self.answer else None

    def approve(self, action: str, risk: str, reason: str) -> bool:
        self.approvals.append(action)
        return self.approve_all


class QueueUser:
    """Bridges the run thread to the web console.

    `on_request` publishes the request (as an event); the HTTP handler calls
    `reply()`, which unblocks the waiting run thread.
    """

    def __init__(self, on_request: Callable[[dict], None], timeout_s: float = 900):
        self.on_request = on_request
        self.timeout_s = timeout_s
        self._pending: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def _wait(self, payload: dict):
        request_id = uuid.uuid4().hex[:8]
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = q
        self.on_request({"request_id": request_id, **payload})
        try:
            return q.get(timeout=self.timeout_s)
        except queue.Empty:
            return None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def reply(self, request_id: str, value) -> bool:
        with self._lock:
            q = self._pending.get(request_id)
        if not q:
            return False
        q.put(value)
        return True

    def ask(self, question: str, options: list[str] | None) -> str | None:
        answer = self._wait({"kind": "question", "question": question, "options": options or []})
        return answer or None

    def approve(self, action: str, risk: str, reason: str) -> bool:
        return self._wait({"kind": "approval", "action": action, "risk": risk, "reason": reason}) is True
