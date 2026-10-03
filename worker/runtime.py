"""Shared state for one task run, and the per-agent context handed to tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import EnvironmentProfile, Settings
from .events import EventBus
from .secrets import SecretVault
from .user import UserChannel

if TYPE_CHECKING:
    from .tools.browser import BrowserManager, BrowserTab


@dataclass
class RunState:
    settings: Settings
    profile: EnvironmentProfile
    bus: EventBus
    vault: SecretVault
    user: UserChannel
    run_dir: Path
    # Working memory: facts the worker chose to record, with provenance.
    memory: dict[str, dict] = field(default_factory=dict)
    plan: list[dict] = field(default_factory=list)
    # Every state-changing action and its observed outcome (applied / rejected / unknown).
    ledger: list[dict] = field(default_factory=list)
    usage: dict[str, float] = field(default_factory=lambda: {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "cost_usd": 0.0, "llm_calls": 0,
    })
    _browser: "BrowserManager | None" = None

    @property
    def workspace(self) -> Path:
        return self.settings.workspace

    def browser(self) -> "BrowserManager":
        if self._browser is None:
            from .tools.browser import BrowserManager

            self._browser = BrowserManager(self)
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None


@dataclass
class ToolContext:
    run: RunState
    agent: str  # "worker" or "verifier"; also selects the browser tab
    step: int = 0

    @property
    def tab(self) -> "BrowserTab":
        return self.run.browser().tab(self.agent)

    def emit(self, type: str, data: dict) -> None:
        self.run.bus.emit(type, data, agent=self.agent, step=self.step)
