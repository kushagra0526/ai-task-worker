"""Action risk classification and the approval gate.

Tools don't decide whether they're allowed to run - they only report how risky
a specific invocation is. The policy maps (risk, mode) to allow / ask / deny.
This keeps safety decisions in one auditable place, independent of what the
model believes, and lets the same tools serve a read-only verifier.
"""

from __future__ import annotations

import re
from enum import Enum


class Risk(str, Enum):
    READ = "read"          # observes state only
    WRITE = "write"        # changes internal state (submit a form, create a record)
    EXTERNAL = "external"  # irreversible or leaves the org: send, pay, delete, approve


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


MODES: dict[str, dict[Risk, Decision]] = {
    # Default: do internal work the task asks for, but a human confirms anything
    # irreversible or outward-facing.
    "guarded": {Risk.READ: Decision.ALLOW, Risk.WRITE: Decision.ALLOW, Risk.EXTERNAL: Decision.ASK},
    "strict": {Risk.READ: Decision.ALLOW, Risk.WRITE: Decision.ASK, Risk.EXTERNAL: Decision.ASK},
    "autonomous": {Risk.READ: Decision.ALLOW, Risk.WRITE: Decision.ALLOW, Risk.EXTERNAL: Decision.ALLOW},
    "read_only": {Risk.READ: Decision.ALLOW, Risk.WRITE: Decision.DENY, Risk.EXTERNAL: Decision.DENY},
}

# Words on a control that signal an irreversible or outbound effect. Used to
# escalate a click from WRITE to EXTERNAL regardless of which app it's in.
EXTERNAL_ACTION = re.compile(
    r"\b(send|pay|payment|transfer|wire|delete|remove|approve|authori[sz]e|publish|purchase|buy|refund|cancel)\b",
    re.IGNORECASE,
)


class ApprovalPolicy:
    def __init__(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"Unknown approval mode '{mode}'. Choose from: {', '.join(MODES)}")
        self.mode = mode

    def decide(self, risk: Risk) -> Decision:
        return MODES[self.mode][risk]


def classify_control(label: str, submits_form: bool, form_method: str | None) -> Risk:
    """Risk of activating a UI control, from generic DOM facts (no app knowledge)."""
    if EXTERNAL_ACTION.search(label or ""):
        return Risk.EXTERNAL
    if submits_form and (form_method or "get").lower() != "get":
        return Risk.WRITE
    return Risk.READ


def classify_http(method: str) -> Risk:
    method = method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return Risk.READ
    if method == "DELETE":
        return Risk.EXTERNAL
    return Risk.WRITE
