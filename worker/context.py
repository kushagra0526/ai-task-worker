"""Context building for token-budgeted models.

The full transcript is always kept (for the trace, the report and providers
with prompt caching). For a model with a per-request token budget, each request
is built as a *view* of it:

* the task message, verbatim;
* the most recent turns, verbatim (the model must see exactly what it just did);
* every older turn reduced to a one-line digest (action -> outcome, URL, alerts);
* the current plan and working memory.

Working memory is what makes this safe: the worker is told older steps are
summarised, so anything it needs later must be recorded with `remember`.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# Results up to this size are kept whole in digests (an API answer, a one-page invoice); larger ones
# (page snapshots) are reduced to their first line, URL and alerts.
SMALL_RESULT_CHARS = 900

# Turns that only update the agent's own bookkeeping; they don't count toward the verbatim window.
BOOKKEEPING_TOOLS = {"remember", "update_plan"}

TRUNCATION_NOTE = "\n... [truncated to fit the context budget; call browser_snapshot with find='...' to see more]"


def _field(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == "text")


def group_turns(messages: list[dict]) -> list[list[dict]]:
    """An assistant message plus the user message(s) answering it form one turn."""
    turns: list[list[dict]] = []
    for m in messages:
        if m["role"] == "assistant" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return turns


def is_bookkeeping(turn: list[dict]) -> bool:
    assistant = turn[0]
    if assistant["role"] != "assistant" or not isinstance(assistant["content"], list):
        return False
    names = [_field(b, "name") for b in assistant["content"] if _field(b, "type") == "tool_use"]
    return bool(names) and all(n in BOOKKEEPING_TOOLS for n in names)


def window_start(turns: list[list[dict]], observations: int) -> int:
    """Index of the first verbatim turn: enough recent turns to include `observations` non-bookkeeping turns."""
    seen, start = 0, len(turns)
    for i in range(len(turns) - 1, -1, -1):
        start = i
        if not is_bookkeeping(turns[i]):
            seen += 1
            if seen >= observations:
                break
    return start


def summarise_result(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= SMALL_RESULT_CHARS:  # API answers, short documents and confirmations survive whole
        return compact or "(no output)"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    parts = [lines[0]]
    for ln in lines[1:]:
        if ln.startswith(("URL:", "[alert]", "WARNING", "HTTP ", "Downloaded")) and ln not in parts:
            parts.append(ln)
    return _clip(" | ".join(parts), 320)


def digest_turn(number: int, turn: list[dict]) -> str:
    calls: dict[str, tuple[str, dict]] = {}
    parts: list[str] = []
    for m in turn:
        if m["role"] == "assistant":
            content = m["content"] if isinstance(m["content"], list) else []
            for b in content:
                if _field(b, "type") == "tool_use":
                    calls[_field(b, "id")] = (_field(b, "name"), _field(b, "input") or {})
        elif isinstance(m["content"], str):
            parts.append("note: " + _clip(m["content"], 300))
        else:
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    name, args = calls.get(b["tool_use_id"], ("?", {}))
                    err = "ERROR " if b.get("is_error") else ""
                    parts.append(f"{name}({_clip(json.dumps(args, ensure_ascii=False), 120)}) -> {err}"
                                 f"{summarise_result(_result_text(b))}")
                elif b.get("type") == "text":
                    parts.append("harness: " + _clip(b["text"], 160))
    return f"{number}. " + "; ".join(parts)


def render_state(plan: list[dict] | None, memory: dict, digests: list[str]) -> str:
    lines = ["[harness] To keep requests small, older steps are summarised. Anything you will need later must be in "
             "working memory - record it with `remember`."]
    if plan:
        lines.append("Current plan:")
        lines += [f"  [{s['status']}] {s['title']}" for s in plan]
    lines.append("Working memory:")
    lines += [f"  - {k} = {v['value']}  (source: {v['source']})" for k, v in memory.items()] or ["  (empty)"]
    if digests:
        lines.append("Earlier steps (summarised):")
        lines += [f"  {d}" for d in digests]
    return "\n".join(lines)


def _with_note(message: dict, note: str) -> dict:
    content = message["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    return {**message, "content": blocks + [{"type": "text", "text": note}]}


def _truncate_results(message: dict, max_chars: int) -> dict:
    """Shorten the tool results in one message so their combined text fits max_chars."""
    if isinstance(message["content"], str):
        return message
    results = [b for b in message["content"] if b.get("type") == "tool_result"]
    share = max(400, max_chars // max(1, len(results)))
    blocks = []
    for b in message["content"]:
        if b.get("type") == "tool_result":
            text = _result_text(b)
            if len(text) > share:
                b = {**b, "content": [{"type": "text", "text": text[:share] + TRUNCATION_NOTE}]}
        blocks.append(b)
    return {**message, "content": blocks}


def build_view(messages: list[dict], budget: int, count: Callable[[str], int], *, plan: list[dict] | None,
               memory: dict, recent_turns: int = 3) -> tuple[list[dict], dict]:
    """Return (messages for the next request, stats). Never mutates `messages`."""
    head, turns = messages[0], group_turns(messages[1:])

    def assemble(keep: int, digests: list[str]) -> list[dict]:
        recent = [m for t in turns[len(turns) - keep:] for m in t] if keep else []
        view = [head] + recent
        note = render_state(plan, memory, digests)
        view[-1] = _with_note(view[-1], note)
        return view

    def size(view: list[dict]) -> int:
        return count(json.dumps(view, ensure_ascii=False, default=str))

    keep = len(turns) - window_start(turns, recent_turns) if turns else 0
    digests = [digest_turn(i + 1, t) for i, t in enumerate(turns[: len(turns) - keep])]
    view = assemble(keep, digests)
    # 1) fewer verbatim turns (always keep the latest), 2) drop the oldest digests, 3) truncate the latest results.
    while size(view) > budget and keep > 1:
        keep -= 1
        digests = [digest_turn(i + 1, t) for i, t in enumerate(turns[: len(turns) - keep])]
        view = assemble(keep, digests)
    dropped = 0
    while size(view) > budget and digests:
        digests = digests[1:]
        dropped += 1
        view = assemble(keep, (["(older steps omitted)"] if dropped else []) + digests)
    if size(view) > budget and turns:
        overflow_chars = (size(view) - budget) * 4
        last = view[-1]
        text_len = sum(len(_result_text(b)) for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result")
        view[-1] = _truncate_results(last, max(400, text_len - overflow_chars))
    return view, {"turns_verbatim": keep, "turns_summarised": len(turns) - keep, "est_tokens": size(view),
                  "budget": budget}
