"""Web console: give the worker a task, watch it work, answer its questions.

Each run executes in its own thread (Playwright's sync API is per-thread);
events stream to the browser over Server-Sent Events, and approvals/questions
come back through `QueueUser`.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from worker.cli import load_tasks
from worker.config import Settings
from worker.orchestrator import run_task
from worker.user import QueueUser

STATIC = Path(__file__).parent / "static"
SCENARIOS = {
    "normal": {"flaky_submit": False, "session_timeout_after": 0},
    "flaky_submit": {"flaky_submit": True, "session_timeout_after": 0},
    "session_timeout": {"flaky_submit": False, "session_timeout_after": 3},
}

app = FastAPI(title="AI Task Worker console")
RUNS: dict[str, "ConsoleRun"] = {}
base_settings = Settings()
base_settings.runs_dir.mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=str(base_settings.runs_dir)), name="runs")


class ConsoleRun:
    def __init__(self, task: str):
        self.id = uuid.uuid4().hex[:8]
        self.task = task
        self.events: list[dict] = []
        self.done = False
        self.cond = threading.Condition()
        self.user = QueueUser(on_request=lambda req: self.push("user_input_requested", req))

    def push(self, type: str, data: dict, agent: str = "worker", step: int = 0) -> None:
        with self.cond:
            self.events.append({"type": type, "data": data, "agent": agent, "step": step, "seq": len(self.events)})
            self.cond.notify_all()

    def on_event(self, event) -> None:
        e = asdict(event)
        self.push(e["type"], e["data"], e["agent"], e["step"])


class RunRequest(BaseModel):
    task: str
    approval_mode: str = "guarded"
    scenario: str = "normal"
    reset: bool = True
    headed: bool = False


class Reply(BaseModel):
    request_id: str
    value: str | bool | None


def _prepare_environment(settings: Settings, req: RunRequest) -> None:
    """Reset the simulated company and apply the chosen failure scenario."""
    base = f"http://{settings.profile().allowed_hosts[0]}"
    try:
        if req.reset:
            httpx.post(base + "/_admin/reset", timeout=5)
        httpx.post(base + "/_admin/chaos", json=SCENARIOS.get(req.scenario, SCENARIOS["normal"]), timeout=5)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Mock environment not reachable at {base} - start it with `python -m mock_env`. ({e})")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/examples")
def examples():
    return load_tasks()


@app.post("/api/runs")
def start_run(req: RunRequest):
    if not req.task.strip():
        raise HTTPException(400, "Task is empty")
    settings = Settings(approval_mode=req.approval_mode, headless=not req.headed)
    _prepare_environment(settings, req)
    run = ConsoleRun(req.task.strip())
    RUNS[run.id] = run

    def target():
        try:
            run_task(run.task, settings, run.user, listeners=[run.on_event])
        except Exception as e:  # surface setup failures (e.g. missing API key) in the UI
            run.push("error", {"message": f"{type(e).__name__}: {e}"})
            run.push("run_finished", {"status": "failed", "summary": str(e), "usage": {}, "result": None})
        finally:
            with run.cond:
                run.done = True
                run.cond.notify_all()

    threading.Thread(target=target, daemon=True, name=f"run-{run.id}").start()
    return {"id": run.id}


@app.get("/api/runs/{run_id}/events")
def stream(run_id: str, after: int = -1, last_event_id: str | None = Header(None)):
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    if last_event_id and last_event_id.isdigit():  # browser reconnect: resume, don't replay
        after = max(after, int(last_event_id))

    def gen():
        cursor = after + 1
        while True:
            with run.cond:
                if cursor >= len(run.events) and not run.done:
                    run.cond.wait(timeout=15)
                batch = run.events[cursor:]
                finished = run.done
            if not batch:
                if finished:
                    yield "event: end\ndata: {}\n\n"
                    return
                yield ": keepalive\n\n"
                continue
            for e in batch:
                yield f"data: {json.dumps(e, default=str)}\n\n"
            cursor += len(batch)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/reply")
def reply(run_id: str, body: Reply):
    run = RUNS.get(run_id)
    if not run or not run.user.reply(body.request_id, body.value):
        raise HTTPException(404, "No pending request with that id")
    run.push("user_input_resolved", {"request_id": body.request_id, "value": body.value})
    return {"ok": True}


def serve(port: int = 8800) -> None:
    print(f"Console: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
