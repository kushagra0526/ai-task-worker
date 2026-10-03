"""Model access behind a narrow interface.

The agent loop depends only on the `LLM` protocol. Two production adapters:

* `AnthropicLLM` - Claude via the official SDK. Sends the full transcript every
  turn; prompt caching makes the repeated prefix cheap.
* `OpenAICompatLLM` - Groq (or any OpenAI-compatible chat API). No prompt
  caching and, on free tiers, a hard tokens-per-minute cap, so it exposes a
  per-request token budget that the agent loop's context builder respects, and
  it paces itself from the server's rate-limit headers.

`ScriptedLLM` lets tests drive the real harness deterministically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol

import anthropic
import httpx

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# USD per million tokens: (input, output). For Claude, cache reads bill at
# 0.1x input and cache writes at 1.25x. Groq figures are list prices for its
# paid tier (the free tier costs nothing). Unknown models are not costed.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
}

Notify = Callable[[str, dict], None]


class LLMError(Exception):
    """The model could not be reached or rejected the request."""


class ContextTooLarge(LLMError):
    """The request exceeded the provider's size limit; retry with a smaller context."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    content: list[Any]  # blocks appended to history unchanged (keeps Claude thinking signatures intact)
    text: str
    thinking: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


class LLM(Protocol):
    model: str
    supports_images: bool

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...

    def message_budget(self, system: str, tools: list[dict]) -> int | None:
        """Tokens available for the conversation in the next request, or None to send it all."""

    def count_tokens(self, text: str) -> int: ...


def estimate_cost(model: str, usage: dict[str, int]) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (
        usage.get("input_tokens", 0) * price_in
        + usage.get("cache_read_input_tokens", 0) * price_in * 0.1
        + usage.get("cache_creation_input_tokens", 0) * price_in * 1.25
        + usage.get("output_tokens", 0) * price_out
    ) / 1_000_000


def make_llm(settings: "Settings", role: str = "worker") -> LLM:
    model = settings.model if role == "worker" else settings.verifier_model
    if settings.provider == "groq":
        others = [m for m in settings.fallback_models if m != model]
        return OpenAICompatLLM(
            model, api_key=os.getenv("GROQ_API_KEY", ""),
            base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            reasoning_effort=settings.effort, fallback_models=others,
        )
    return AnthropicLLM(model, settings.effort, settings.refusal_fallback)


# ---------------------------------------------------------------------- Claude


class AnthropicLLM:
    supports_images = True

    def __init__(self, model: str, effort: str = "high", refusal_fallback: bool = True, max_tokens: int = 32000):
        self.model = model
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self.max_tokens = max_tokens
        # SDK retries 408/409/429/5xx and connection errors with backoff.
        self.client = anthropic.Anthropic(max_retries=4)

    def message_budget(self, system: str, tools: list[dict]) -> int | None:
        return None  # 1M context + prompt caching: send the full transcript

    def count_tokens(self, text: str) -> int:
        return len(text) // 4 + 1

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


# ---------------------------------------------------------------------- Groq / OpenAI-compatible


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


def _field(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    else:
        text = "\n".join(c.get("text", "") if c.get("type") == "text" else "[image omitted]" for c in content or [])
    return ("ERROR: " + text) if block.get("is_error") else text


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the canonical (Anthropic-shaped) transcript into chat-completions messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
            text = "\n".join(_field(b, "text") for b in blocks if _field(b, "type") == "text" and _field(b, "text"))
            calls = [{"id": _field(b, "id"), "type": "function",
                      "function": {"name": _field(b, "name"), "arguments": json.dumps(_field(b, "input") or {})}}
                     for b in blocks if _field(b, "type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": text or (None if calls else "(no output)")}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            texts = []
            for block in content:
                if block.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": _result_text(block)})
                elif block.get("type") == "text":
                    texts.append(block["text"])
            if texts:  # harness notices / state go after the tool messages they follow
                out.append({"role": "user", "content": "\n\n".join(texts)})
    return out


_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?")


def parse_duration(text: str) -> float | None:
    """Groq durations look like '7.66s', '1m26.4s', '393ms'."""
    m = _DURATION.fullmatch(text.strip())
    if not m or not any(m.groups()):
        return None
    h, mins, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mins or 0) * 60 + float(s or 0) + float(ms or 0) / 1000


class OpenAICompatLLM:
    """Groq's OpenAI-compatible chat completions API, built for tight rate limits."""

    supports_images = False  # the gpt-oss models are text-only
    MAX_WAIT_S = 90  # longer waits than this mean a daily quota, not a per-minute one

    def __init__(self, model: str, *, api_key: str, base_url: str, reasoning_effort: str = "medium",
                 fallback_models: list[str] | None = None, max_completion_tokens: int = 8192,
                 default_token_limit: int = 8000, notify: Notify | None = None):
        self.model = model
        self.api_key = api_key
        self.client = httpx.Client(base_url=base_url, timeout=120)
        self.reasoning_effort = reasoning_effort
        self.fallback_models = list(fallback_models or [])
        self.max_completion_tokens = max_completion_tokens
        self.notify: Notify = notify or (lambda type, data: None)
        # Learned from x-ratelimit-* headers; until then assume a free-tier-sized budget.
        self.token_limit = default_token_limit
        self._remaining: float | None = None
        self._seen_at = 0.0
        # Share of the per-minute limit one request may use. Shrinks if the server says a request is too large.
        self.ceiling = 0.8
        # Calibrated from real usage after every call (starts conservative).
        self.chars_per_token = 3.8
        self.expected_completion = 700.0

    # ------------------------------------------------------------- budgeting

    def count_tokens(self, text: str) -> int:
        return int(len(text) / self.chars_per_token) + 1

    def message_budget(self, system: str, tools: list[dict]) -> int:
        request_cap = min(self.token_limit * self.ceiling, 100_000)  # 131k context window, keep headroom
        fixed = self.count_tokens(system + json.dumps(to_openai_tools(tools)))
        return max(800, int(request_cap - fixed - self.expected_completion))

    def shrink(self) -> None:
        self.ceiling = max(0.3, self.ceiling * 0.75)

    def _pace(self, needed: float) -> None:
        """Wait until the token bucket can cover this request instead of collecting 429s."""
        if self._remaining is None:
            return
        needed = min(needed, self.token_limit)  # a request larger than the bucket only needs a full bucket
        rate = self.token_limit / 60.0
        available = min(self.token_limit, self._remaining + (time.time() - self._seen_at) * rate)
        if available < needed:
            wait = min((needed - available) / rate, 60.0)
            self.notify("rate_limit_wait", {"seconds": round(wait, 1), "model": self.model, "reason": "tokens per minute"})
            time.sleep(wait)

    def _learn_limits(self, response: httpx.Response) -> None:
        h = response.headers
        if h.get("x-ratelimit-limit-tokens", "").isdigit():
            self.token_limit = int(h["x-ratelimit-limit-tokens"])
        if h.get("x-ratelimit-remaining-tokens", "").lstrip("-").isdigit():
            self._remaining = float(h["x-ratelimit-remaining-tokens"])
            self._seen_at = time.time()

    def _retry_after(self, response: httpx.Response, message: str) -> float:
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        m = re.search(r"try again in ([\dhms.]+)", message)
        return (parse_duration(m.group(1)) if m else None) or 5.0

    def _switch_model(self, reason: str) -> bool:
        if not self.fallback_models:
            return False
        previous, self.model = self.model, self.fallback_models.pop(0)
        self._remaining = None
        self.notify("model_switched", {"from": previous, "to": self.model, "reason": reason})
        return True

    # ------------------------------------------------------------- request

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self.api_key:
            raise LLMError("No Groq API key found - set GROQ_API_KEY (see .env.example).")
        chat = to_openai_messages(system, messages)
        oa_tools = to_openai_tools(tools)
        chars = len(json.dumps(chat)) + len(json.dumps(oa_tools))
        corrections = 0
        transient = 0
        while True:
            payload: dict = {"model": self.model, "messages": chat, "tools": oa_tools,
                             "max_completion_tokens": self.max_completion_tokens}
            if self.model.startswith("openai/gpt-oss"):
                payload["reasoning_effort"] = self.reasoning_effort
            self._pace(chars / self.chars_per_token + self.expected_completion)
            try:
                response = self.client.post("/chat/completions", json=payload,
                                            headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.TransportError as e:
                transient += 1
                if transient > 3:
                    raise LLMError(f"Could not reach Groq: {type(e).__name__}") from e
                time.sleep(2 ** transient)
                continue
            self._learn_limits(response)
            status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {}
            error = body.get("error") or {}
            message = str(error.get("message", response.text[:300]))

            if status == 200:
                parsed = self._parse(body, chars)
                if parsed is not None:
                    return parsed
                error = {"code": "tool_use_failed", "message": "tool call arguments were not valid JSON"}
                status = 400
            if status in (401, 403):
                raise LLMError("Groq authentication failed - check GROQ_API_KEY.")
            if status == 413 or (status == 400 and ("too large" in message.lower() or "context_length" in message.lower())):
                raise ContextTooLarge(message)
            if status == 400 and error.get("code") == "tool_use_failed":
                # The model produced a malformed tool call. Ask again with an explicit correction.
                corrections += 1
                if corrections > 2:
                    raise LLMError("The model repeatedly produced malformed tool calls.")
                self.notify("model_retry", {"reason": "malformed tool call", "model": self.model})
                chat = chat + [{"role": "user", "content": (
                    "[harness] Your previous reply was not a valid tool call. Reply with a tool call whose name is one of "
                    "the provided tools and whose arguments are a valid JSON object matching its schema.")}]
                continue
            if status == 429:
                wait = self._retry_after(response, message)
                if "per day" in message.lower() or wait > self.MAX_WAIT_S:
                    if self._switch_model(f"daily quota reached on {self.model}"):
                        continue
                    raise LLMError(f"Groq daily quota exhausted for {self.model}: {message[:200]}")
                self.notify("rate_limit_wait", {"seconds": round(wait, 1), "model": self.model, "reason": "429"})
                time.sleep(wait)
                continue
            if status >= 500:
                transient += 1
                if transient > 3:
                    raise LLMError(f"Groq server error {status} persisted after retries.")
                time.sleep(2 ** transient)
                continue
            raise LLMError(f"Groq rejected the request ({status}): {message[:300]}")

    def _parse(self, body: dict, chars_sent: int) -> LLMResponse | None:
        choice = body["choices"][0]
        msg = choice["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                return None
            if not isinstance(args, dict):
                return None
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args))
        usage = body.get("usage") or {}
        prompt_tokens, completion_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        if prompt_tokens:  # calibrate the char->token estimate from what the server actually counted
            ratio = min(6.5, max(2.5, chars_sent / prompt_tokens))
            self.chars_per_token = 0.6 * self.chars_per_token + 0.4 * ratio
        if completion_tokens:
            self.expected_completion = 0.7 * self.expected_completion + 0.3 * completion_tokens
        text = (msg.get("content") or "").strip()
        content: list[dict] = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        if not content:
            content = [{"type": "text", "text": "(empty response)"}]
        stop = "tool_use" if calls else {"length": "max_tokens"}.get(choice.get("finish_reason"), "end_turn")
        return LLMResponse(content=content, text=text, thinking=(msg.get("reasoning") or "").strip(), tool_calls=calls,
                           stop_reason=stop, usage={"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
                           model=body.get("model", self.model))


# ---------------------------------------------------------------------- testing

ScriptStep = Callable[[list[dict]], list[tuple[str, dict]] | str]


class ScriptedLLM:
    """Deterministic stand-in for tests: each step inspects the transcript and
    returns tool calls [(name, input), ...] or plain text. Set `budget` to make
    the agent loop build compacted contexts, as it does for Groq."""

    supports_images = True

    def __init__(self, steps: list[ScriptStep], model: str = "scripted", budget: int | None = None):
        self.steps = list(steps)
        self.model = model
        self.budget = budget
        self.calls = 0
        self.seen: list[list[dict]] = []

    def message_budget(self, system: str, tools: list[dict]) -> int | None:
        return self.budget

    def count_tokens(self, text: str) -> int:
        return len(text) // 4 + 1

    def respond(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        if not self.steps:
            raise LLMError("ScriptedLLM ran out of steps")
        self.seen.append(messages)
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
