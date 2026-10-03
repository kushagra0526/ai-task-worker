"""Tool abstraction shared by every capability (browser, files, HTTP, control)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..policy import Risk

if TYPE_CHECKING:
    from ..runtime import ToolContext


class ToolError(Exception):
    """An expected failure the model should see and react to (not a harness bug)."""


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    images: list[bytes] = field(default_factory=list)  # PNGs shown to the model
    evidence: list[str] = field(default_factory=list)  # artifact paths relative to the run dir
    meta: dict[str, Any] = field(default_factory=dict)


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}
    risk: Risk = Risk.READ

    def classify(self, args: dict, ctx: "ToolContext") -> Risk:
        """Risk of this specific invocation. Override for dynamic classification."""
        return self.risk

    def describe_action(self, args: dict, ctx: "ToolContext") -> str:
        """Human-readable description used in approval prompts."""
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"

    def fingerprint(self, args: dict, ctx: "ToolContext") -> str:
        """Identity of the side effect a state-changing call would cause, used to detect repeats."""
        return f"{self.name} {json.dumps(args, sort_keys=True)}"

    def origin(self, args: dict, ctx: "ToolContext") -> str:
        """Where a state-changing call is made from (a page URL, an endpoint)."""
        return ""

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        raise NotImplementedError

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate_args(schema: dict, args: Any) -> list[str]:
    """Small JSON-schema subset check: required keys, top-level types, enums.

    The model is usually right, but a malformed call must become a readable
    error for the model rather than an exception deep inside a tool.
    """
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    errors = []
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            errors.append(f"missing required argument '{key}'")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                errors.append(f"unexpected argument '{key}'")
            continue
        expected = _TYPES.get(spec.get("type", ""))
        if expected and (not isinstance(value, expected) or (spec.get("type") != "boolean" and isinstance(value, bool))):
            errors.append(f"argument '{key}' must be of type {spec['type']}")
        elif "enum" in spec and value not in spec["enum"]:
            errors.append(f"argument '{key}' must be one of {spec['enum']}")
    return errors
