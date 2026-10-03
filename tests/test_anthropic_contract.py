"""Contract tests for the production model path (`AnthropicLLM` + the real SDK)
against a local fake of the Messages API that speaks the real SSE protocol."""

import pytest
from conftest import BASE
from fake_anthropic import FakeAnthropic

from worker.llm import AnthropicLLM, last_tool_text
from worker.orchestrator import run_task
from worker.user import AutoUser

FAKE_PORT = 8792


@pytest.fixture(scope="module")
def fake_server():
    fake = FakeAnthropic(FAKE_PORT).start()
    yield fake
    fake.stop()


@pytest.fixture
def fake_api(fake_server, monkeypatch):
    fake_server.requests.clear()
    fake_server.faults.clear()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake_server.base_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return fake_server


def scripted_model(body: dict) -> list[dict]:
    turn = sum(1 for m in body["messages"] if m["role"] == "assistant")
    if body["system"].startswith("You are an independent verifier"):
        assert "Original task given to the worker" in last_tool_text(body["messages"][:1])
        return [{"type": "tool_use", "name": "submit_verdict", "input": {
            "verdict": "verified", "summary": "Checked.", "checks": [{"claim": "inbox reachable", "observed": "yes", "passed": True}]}}]
    if turn == 0:
        return [{"type": "thinking", "thinking": "I should look at the inbox first."},
                {"type": "text", "text": "Opening the inbox."},
                {"type": "tool_use", "name": "browser_navigate", "input": {"url": f"{BASE}/mail"}}]
    if turn == 1:
        return [{"type": "tool_use", "name": "browser_screenshot", "input": {}}]
    return [{"type": "tool_use", "name": "finish", "input": {
        "status": "success", "summary": "Inbox inspected.", "result": {"emails": "8"},
        "evidence": [{"claim": "inbox has 8 emails", "source": f"{BASE}/mail"}], "self_check": "screenshot"}}]


def test_full_run_through_real_sdk_with_api_faults(env, fake_api):
    settings, vault = env
    fake_api.responder = scripted_model
    # 1st request: the server rejects the refusal-fallback beta -> the client must disable it and retry.
    fake_api.fail_next(400, "invalid_request_error", "fallbacks: not available for this organization")
    # Then transient faults the SDK's retry policy must absorb.
    fake_api.fail_next(429, "rate_limit_error", "slow down", headers={"retry-after-ms": "10"})
    fake_api.fail_next(500, "api_error", "internal error")

    llm = AnthropicLLM("claude-opus-5", effort="high", refusal_fallback=True)
    result = run_task("Look at the AP inbox.", settings, AutoUser(), llm=llm, vault=vault)

    assert result.status == "completed_verified", result.summary
    reqs = fake_api.requests
    # Fallback beta was attempted, then dropped.
    assert "server-side-fallback-2026-07-01" in reqs[0]["headers"].get("anthropic-beta", "")
    assert reqs[0]["body"]["fallbacks"] == "default"
    assert "fallbacks" not in reqs[1]["body"] and llm.refusal_fallback is False
    # 400 + 429 + 500 + 3 worker turns + 1 verifier turn
    assert len(reqs) == 7

    body = reqs[3]["body"]  # first successful worker request
    assert body["model"] == "claude-opus-5"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert {"browser_navigate", "finish", "remember", "ask_user"} <= {t["name"] for t in body["tools"]}
    assert "{environment}" not in body["system"] and "Northwind" in body["system"]

    # Second turn must replay the assistant turn unchanged (thinking signature intact) and answer the tool call.
    msgs = reqs[4]["body"]["messages"]
    assistant = msgs[1]["content"]
    assert [b["type"] for b in assistant] == ["thinking", "text", "tool_use"]
    assert assistant[0]["signature"].startswith("sig-") and assistant[0]["thinking"]
    tool_result = msgs[2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == assistant[2]["id"]
    assert "Northwind Mail" in tool_result["content"][0]["text"]

    # Screenshot tool results carry an image block back to the model.
    shot = reqs[5]["body"]["messages"][-1]["content"][0]["content"]
    assert any(c["type"] == "image" and c["source"]["media_type"] == "image/png" for c in shot)

    # Usage and cost accounting from the streamed usage fields.
    assert result.usage["llm_calls"] == 4
    assert result.usage["cache_read_input_tokens"] == 16000
    assert result.usage["cost_usd"] > 0
    events = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "I should look at the inbox first." in events


def test_authentication_failure_ends_run_cleanly(env, fake_api):
    settings, vault = env
    fake_api.fail_next(401, "authentication_error", "invalid x-api-key")
    result = run_task("Anything.", settings, AutoUser(), llm=AnthropicLLM("claude-opus-5", refusal_fallback=False),
                      vault=vault)
    assert result.status == "failed"
    assert "Authentication failed" in result.summary
    assert result.report_path.exists()
