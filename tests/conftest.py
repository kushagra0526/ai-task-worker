import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from mock_env import data
from worker.config import Settings
from worker.secrets import SecretVault

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session")
def mock_server():
    config = uvicorn.Config("mock_env.app:app", host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(BASE + "/", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield BASE
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def env(mock_server, tmp_path):
    """Fresh mock state + a settings object pointing at temp workspace/runs."""
    httpx.post(mock_server + "/_admin/reset")
    profile = json.loads((Path(__file__).parent.parent / "environment.json").read_text())
    profile["allowed_hosts"] = [f"127.0.0.1:{PORT}"]
    for app in profile["applications"]:
        app["url"] = app["url"].replace("8700", str(PORT))
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "vendor_categories.csv").write_text(
        (Path(__file__).parent.parent / "workspace" / "vendor_categories.csv").read_text())
    settings = Settings(profile_path=profile_path, workspace=workspace, runs_dir=tmp_path / "runs",
                        approval_mode="guarded", max_steps=40)
    vault = SecretVault.from_values({
        "erp_password": data.ERP_PASSWORD, "erp_api_key": data.ERP_API_KEY})
    return settings, vault


def chaos(**kwargs):
    httpx.post(BASE + "/_admin/chaos", json=kwargs)


def mock_state() -> dict:
    return httpx.get(BASE + "/_admin/state").json()


def find_ref(text: str, pattern: str) -> str:
    """Return the ref of the first element line matching `pattern` (regex), e.g. r'link "Your Acme.*0917'."""
    m = re.search(r"\[(e\d+) " + pattern, text)
    assert m, f"no element matching {pattern!r} in:\n{text[:3000]}"
    return m.group(1)
