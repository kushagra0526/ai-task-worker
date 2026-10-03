"""Every state change the worker attempts, with the outcome the harness observed.

Used to stop blind repeats. After an unknown outcome (5xx, no response) an identical
submission is refused until the worker has looked at the state somewhere else; after
a success, repeating it needs a human yes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tools import ToolResult


@dataclass
class Entry:
    step: int
    agent: str
    tool: str
    fingerprint: str  # method + target + field values: what the change would send
    origin: str       # page or endpoint it was sent from
    outcome: str      # applied | rejected | unknown
    detail: str
    checked: bool = False  # unknown outcome: state has since been observed elsewhere


class Ledger:
    def __init__(self) -> None:
        self.entries: list[Entry] = []

    def last(self, fingerprint: str) -> Entry | None:
        return next((e for e in reversed(self.entries) if e.fingerprint == fingerprint), None)

    def record(self, step: int, agent: str, tool: str, fingerprint: str, origin: str,
               result: ToolResult) -> Entry | None:
        status = result.meta.get("status")
        if result.meta.get("applied"):
            outcome, detail = "applied", "completed locally"
        elif isinstance(status, int):
            outcome = "applied" if status < 400 else "rejected" if status < 500 else "unknown"
            detail = f"HTTP {status}"
        elif result.meta.get("attempted") or not result.is_error:
            outcome, detail = "unknown", "no response observed"
        else:
            return None  # failed before anything was sent
        entry = Entry(step, agent, tool, fingerprint, origin, outcome, detail)
        self.entries.append(entry)
        return entry

    def observe(self, location: str) -> None:
        """A successful read somewhere other than where a change came from counts as checking it."""
        for e in self.entries:
            if e.outcome == "unknown" and not e.checked and location != e.origin:
                e.checked = True
