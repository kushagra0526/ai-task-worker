"""Structured run trace.

Every meaningful thing the worker does is an Event. Events fan out to
listeners (CLI printer, web console) and are appended to `events.jsonl` in the
run directory, which is the source of truth for the report and for debugging.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    agent: str = "worker"
    step: int = 0
    ts: float = field(default_factory=time.time)
    seq: int = 0


Listener = Callable[[Event], None]


class EventBus:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.events: list[Event] = []
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._file = (run_dir / "events.jsonl").open("a", encoding="utf-8")

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def emit(self, type: str, data: dict[str, Any] | None = None, *, agent: str = "worker", step: int = 0) -> Event:
        with self._lock:
            event = Event(type=type, data=data or {}, agent=agent, step=step, seq=len(self.events))
            self.events.append(event)
            self._file.write(json.dumps(asdict(event), default=str) + "\n")
            self._file.flush()
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # a broken UI must never kill the run
                pass
        return event

    def close(self) -> None:
        self._file.close()


def new_run_dir(runs_root: Path, task: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40] or "task"
    run_dir = runs_root / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    (run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    return run_dir
