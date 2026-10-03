"""Model access behind a narrow interface.

The agent loop depends only on `LLM.respond()`, which lets tests drive the
real harness, tools and mock environment with a deterministic scripted model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import anthropic

log = logging.getLogger(__name__)

# USD per million tokens: (input, output). Cache reads bill at 0.1x input,
# cache writes at 1.25x input.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class LLMError(Exception):
    """The model could not be reached or rejected the request."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    content: list[Any]  # raw blocks, appended to history unchanged (keeps thinking signatures intact)
    text: str
    thinking: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


class LLM(Protocol):
    model: str

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


def estimate_cost(model: str, usage: dict[str, int]) -> float:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5"])
    return (
        usage.get("input_tokens", 0) * price_in
        + usage.get("cache_read_input_tokens", 0) * price_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * price_in * 1.25
        + usage.get("output_tokens", 0) * price_out
    ) / 1_000_000


class AnthropicLLM:
    def __init__(self, model: str, effort: str = "high", refusal_fallback: bool = True, max_tokens: int = 32000):
        self.model = model
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self.max_tokens = max_tokens
        # SDK retries 408/409/429/5xx and connection errors with backoff.
        self.client = anthropic.Anthropic(max_retries=4)

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        params = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # caches the growing transcript prefix turn over turn
        )
        try:
            if self.refusal_fallback:
                with self.client.beta.messages.stream(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
                ) as stream:
                    msg = stream.get_final_message()
            else:
                with self.client.messages.stream(**params) as stream:
                    msg = stream.get_final_message()
        except anthropic.BadRequestError as e:
            if self.refusal_fallback and "fallback" in str(e).lower():
                log.warning("Server-side refusal fallback not available; continuing without it.")
                self.refusal_fallback = False
                return self.respond(system, messages, tools)
            raise LLMError(f"Request rejected: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Authentication failed - set ANTHROPIC_API_KEY (see .env.example).") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Rate limited by the API even after retries.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e
        except TypeError as e:  # raised by the SDK when no credentials are configured at all
            if "authentication" not in str(e).lower():
                raise
            raise LLMError("No Anthropic credentials found - set ANTHROPIC_API_KEY (see .env.example).") from e

        text, thinking, calls = [], [], []
        for block in msg.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "thinking":
                thinking.append(block.thinking or "")
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        u = msg.usage
        usage = {
            "input_tokens": u.input_tokens or 0,
            "output_tokens": u.output_tokens or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        return LLMResponse(
            content=list(msg.content), text="\n".join(text).strip(), thinking="\n".join(thinking).strip(),
            tool_calls=calls, stop_reason=msg.stop_reason or "", usage=usage, model=msg.model,
        )


# ---------------------------------------------------------------------- testing

ScriptStep = Callable[[list[dict]], list[tuple[str, dict]] | str]


class ScriptedLLM:
    """Deterministic stand-in for tests: each step inspects the transcript and
    returns tool calls [(name, input), ...] or plain text."""

    def __init__(self, steps: list[ScriptStep], model: str = "scripted"):
        self.steps = list(steps)
        self.model = model
        self.calls = 0

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self.steps:
            raise LLMError("ScriptedLLM ran out of steps")
        out = self.steps.pop(0)(messages)
        self.calls += 1
        if isinstance(out, str):
            return LLMResponse([{"type": "text", "text": out}], out, "", [], "end_turn")
        calls = [ToolCall(f"call_{self.calls}_{i}", name, args) for i, (name, args) in enumerate(out)]
        content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        return LLMResponse(content, "", "", calls, "tool_use")


def last_tool_text(messages: list[dict]) -> str:
    """Helper for scripted steps: concatenated text of the latest tool results."""
    for msg in reversed(messages):
        if msg["role"] == "user" and isinstance(msg["content"], str):
            return msg["content"]
        if msg["role"] == "user" and isinstance(msg["content"], list):
            parts = []
            for block in msg["content"]:
                if block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, str):
                        parts.append(content)
                    else:
                        parts += [c.get("text", "") for c in content if c.get("type") == "text"]
                elif block.get("type") == "text":
                    parts.append(block["text"])
            return "\n".join(parts)
    return ""
