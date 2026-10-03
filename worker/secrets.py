"""Credential handling: the model references secrets, the harness resolves them.

The model writes `{{secret:erp_password}}` in a tool argument; the value is
substituted only at the moment the tool executes and is scrubbed from every
tool result before it re-enters the model context or the trace. Secret values
never appear in prompts, logs, or reports.
"""

from __future__ import annotations

import os
import re

PLACEHOLDER = re.compile(r"\{\{secret:([a-z0-9_]+)\}\}")


class SecretVault:
    def __init__(self, names: dict[str, str], env_prefix: str = "WORKER_SECRET_"):
        self._values: dict[str, str] = {}
        for name in names:
            value = os.getenv(env_prefix + name.upper())
            if value:
                self._values[name] = value

    @classmethod
    def from_values(cls, values: dict[str, str]) -> "SecretVault":
        vault = cls({})
        vault._values = dict(values)
        return vault

    @property
    def available(self) -> list[str]:
        return sorted(self._values)

    def resolve(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in self._values:
                raise KeyError(f"Unknown secret '{name}'. Available: {', '.join(self.available) or 'none'}")
            return self._values[name]

        return PLACEHOLDER.sub(sub, text)

    def mask(self, text: str) -> str:
        for name, value in self._values.items():
            if value and len(value) >= 4:
                text = text.replace(value, f"{{{{secret:{name}}}}}")
        return text
