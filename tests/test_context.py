"""Context builder: budgets are respected, observations stay visible, the transcript is never mutated."""

import copy
import json

from worker.context import build_view, group_turns, window_start


def count(text: str) -> int:
    return len(text) // 4 + 1


def turn(i: int, tool: str, args: dict, result: str) -> list[dict]:
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": args}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                          "content": [{"type": "text", "text": result}]}]}]


PAGE = "URL: http://x/page\nTitle: Page\n---\n" + "\n".join(f"[e{i} link \"Item {i}\" -> /i/{i}]" for i in range(200))
INVOICE = "[page 1]\nAcme Cloud Services\nInvoice number: ACME-2026-0917\nPayment due: 2026-10-30\nTotal due USD 4,812.50"


def transcript() -> list[dict]:
    msgs = [{"role": "user", "content": "Task: enter the Acme invoice."}]
    msgs += turn(1, "browser_navigate", {"url": "http://x/mail"}, PAGE)
    msgs += turn(2, "read_file", {"path": "downloads/inv.pdf"}, INVOICE)
    msgs += turn(3, "remember", {"facts": []}, "Remembered.")
    msgs += turn(4, "update_plan", {"steps": []}, "Plan recorded.")
    msgs += turn(5, "browser_navigate", {"url": "http://x/erp"}, PAGE)
    msgs += turn(6, "browser_click", {"ref": "e3"}, PAGE)
    msgs += turn(7, "browser_click", {"ref": "e4"}, PAGE)
    return msgs


def test_window_skips_bookkeeping_turns():
    turns = group_turns(transcript()[1:])
    # The last three observations are turns 5-7; bookkeeping turns 3-4 sit before them.
    assert window_start(turns, 3) == 4
    assert window_start(turns, 4) == 1  # the 4th observation back is the PDF read; turns 3-4 come along


def test_view_fits_budget_keeps_task_memory_and_small_results():
    msgs = transcript()
    original = copy.deepcopy(msgs)
    memory = {"invoice_amount": {"value": "USD 4812.50", "source": "inv.pdf", "step": 3}}
    plan = [{"title": "Enter bill", "status": "in_progress"}]

    view, stats = build_view(msgs, 2500, count, plan=plan, memory=memory)

    assert msgs == original  # the stored transcript is untouched
    text = json.dumps(view)
    assert count(text) <= 2500 and stats["est_tokens"] <= 2500
    assert view[0]["content"] == "Task: enter the Acme invoice."
    assert "invoice_amount = USD 4812.50" in text and "[in_progress] Enter bill" in text
    assert "Total due USD 4,812.50" in text  # the one-page invoice survives whole in its digest
    assert stats["turns_summarised"] >= 1


def test_tool_calls_and_results_stay_paired():
    view, _ = build_view(transcript(), 1500, count, plan=None, memory={})
    calls = {b["id"] for m in view if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"}
    results = {b["tool_use_id"] for m in view if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"}
    assert calls == results and calls  # never a call without its result, or the reverse


def test_oversized_latest_result_is_truncated_not_dropped():
    msgs = [{"role": "user", "content": "Task."}] + turn(1, "browser_snapshot", {}, PAGE * 5)
    view, stats = build_view(msgs, 1200, count, plan=None, memory={})
    assert stats["turns_verbatim"] == 1
    assert "truncated to fit the context budget" in json.dumps(view)
