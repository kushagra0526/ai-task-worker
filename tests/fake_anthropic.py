"""A local stand-in for the Anthropic Messages API, speaking the real streaming
(SSE) protocol, so the production `AnthropicLLM` path - SDK request building,
stream parsing, thinking signatures, tool calls, retries, error mapping - can be
exercised end to end without network access or an API key."""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# A responder gets the parsed request body and returns content blocks:
# {"type": "thinking", "thinking": str} | {"type": "text", "text": str} | {"type": "tool_use", "name": str, "input": dict}
Responder = Callable[[dict], list[dict]]


class FakeAnthropic:
    def __init__(self, port: int):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.requests: list[dict] = []  # {"headers": {...}, "body": {...}}
        self.faults: list[tuple[int, dict, dict]] = []  # (status, json body, headers) served before normal replies
        self.responder: Responder = lambda body: [{"type": "text", "text": "ok"}]
        self.app = FastAPI()
        self.app.post("/v1/messages")(self._messages)
        self._server: uvicorn.Server | None = None
        self._counter = 0

    def fail_next(self, status: int, error_type: str, message: str, headers: dict | None = None) -> None:
        self.faults.append((status, {"type": "error", "error": {"type": error_type, "message": message}}, headers or {}))

    async def _messages(self, request: Request):
        body = await request.json()
        self.requests.append({"headers": dict(request.headers), "body": body})
        if self.faults:
            status, payload, headers = self.faults.pop(0)
            return JSONResponse(payload, status_code=status, headers=headers)
        blocks = self.responder(body)
        return StreamingResponse(self._stream(body, blocks), media_type="text/event-stream")

    def _stream(self, body: dict, blocks: list[dict]):
        self._counter += 1

        def event(type_: str, data: dict) -> str:
            return f"event: {type_}\ndata: {json.dumps({'type': type_, **data})}\n\n"

        yield event("message_start", {"message": {
            "id": f"msg_fake_{self._counter}", "type": "message", "role": "assistant", "model": body["model"],
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 1, "cache_read_input_tokens": 4000,
                      "cache_creation_input_tokens": 200}}})
        stop_reason = "end_turn"
        for i, block in enumerate(blocks):
            if block["type"] == "thinking":
                yield event("content_block_start", {"index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                yield event("content_block_delta", {"index": i, "delta": {"type": "thinking_delta", "thinking": block["thinking"]}})
                yield event("content_block_delta", {"index": i, "delta": {"type": "signature_delta", "signature": f"sig-{self._counter}-{i}"}})
            elif block["type"] == "text":
                yield event("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}})
                yield event("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": block["text"]}})
            elif block["type"] == "tool_use":
                stop_reason = "tool_use"
                tool_id = f"toolu_{self._counter}_{i}"
                yield event("content_block_start", {"index": i, "content_block": {"type": "tool_use", "id": tool_id, "name": block["name"], "input": {}}})
                raw = json.dumps(block["input"])
                for j in range(0, len(raw), 40):  # split JSON across deltas like the real API
                    yield event("content_block_delta", {"index": i, "delta": {"type": "input_json_delta", "partial_json": raw[j:j + 40]}})
            yield event("content_block_stop", {"index": i})
        yield event("message_delta", {"delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 150}})
        yield event("message_stop", {})

    def start(self) -> "FakeAnthropic":
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(100):
            try:
                httpx.get(self.base_url + "/docs", timeout=0.5)
                return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("fake Anthropic server did not start")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
