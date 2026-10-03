"""Runtime configuration.

Two layers, deliberately separate:

* `Settings` - how the worker runs (model, budgets, approval mode). Env vars / CLI flags.
* `EnvironmentProfile` - *what world* the worker operates in (known apps, allowed
  hosts, secret names). Loaded from a JSON file. Pointing the worker at a different
  company/app means writing a new profile, not changing code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class EnvironmentProfile:
    name: str
    applications: list[dict]
    allowed_hosts: list[str]
    secrets: dict[str, str]  # secret name -> human description (values come from env)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "EnvironmentProfile":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=raw["name"],
            applications=raw.get("applications", []),
            allowed_hosts=raw.get("allowed_hosts", []),
            secrets=raw.get("secrets", {}),
            notes=raw.get("notes", []),
        )

    def describe(self) -> str:
        lines = [f"Organisation: {self.name}", "", "Applications you can use:"]
        for app in self.applications:
            lines.append(f"- {app['name']} ({app['url']}): {app['description']}")
        if self.secrets:
            lines += ["", "Credentials available by reference (you never see the values):"]
            lines += [f"- {{{{secret:{name}}}}} - {desc}" for name, desc in self.secrets.items()]
        if self.notes:
            lines += ["", "Notes:"] + [f"- {n}" for n in self.notes]
        return "\n".join(lines)


PROVIDER_DEFAULTS = {
    # provider: (worker model, verifier model, effort, fallback models on daily-quota exhaustion)
    "anthropic": ("claude-opus-5", "", "high", []),
    # Groq rate limits are per model, so the verifier runs on a different model than the worker: twice the
    # throughput, and a second model judging the first is more independent than self-review.
    "groq": ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "medium", ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]),
}


@dataclass
class Settings:
    provider: str = field(default_factory=lambda: os.getenv("WORKER_PROVIDER", "anthropic"))
    model: str = field(default_factory=lambda: os.getenv("WORKER_MODEL", ""))
    verifier_model: str = field(default_factory=lambda: os.getenv("WORKER_VERIFIER_MODEL", ""))
    effort: str = field(default_factory=lambda: os.getenv("WORKER_EFFORT", ""))
    fallback_models: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.getenv("WORKER_FALLBACK_MODELS", "").split(",") if m.strip()])
    # Server-side refusal fallback (beta). Disabled automatically if the API rejects it.
    refusal_fallback: bool = field(default_factory=lambda: os.getenv("WORKER_REFUSAL_FALLBACK", "1") == "1")
    approval_mode: str = "guarded"  # guarded | strict | autonomous | read_only
    max_steps: int = 40
    verifier_max_steps: int = 15
    max_verification_rounds: int = 2
    verify: bool = True
    headless: bool = True
    profile_path: Path = field(default_factory=lambda: Path(os.getenv("WORKER_PROFILE", ROOT / "environment.json")))
    workspace: Path = field(default_factory=lambda: Path(os.getenv("WORKER_WORKSPACE", ROOT / "workspace")))
    runs_dir: Path = field(default_factory=lambda: Path(os.getenv("WORKER_RUNS_DIR", ROOT / "runs")))

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_DEFAULTS:
            raise ValueError(f"Unknown WORKER_PROVIDER '{self.provider}'. Choose from: {', '.join(PROVIDER_DEFAULTS)}")
        model, verifier_model, effort, fallbacks = PROVIDER_DEFAULTS[self.provider]
        self.model = self.model or model
        self.verifier_model = self.verifier_model or verifier_model or self.model
        self.effort = self.effort or effort
        self.fallback_models = self.fallback_models or list(fallbacks)

    def profile(self) -> EnvironmentProfile:
        return EnvironmentProfile.load(self.profile_path)
