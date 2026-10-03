"""Evidence report for a run: what was asked, what happened, and proof.

Written as both Markdown (diff-able, pasteable) and a self-contained HTML page
with screenshots, generated purely from the event log.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .events import Event
    from .orchestrator import TaskResult

STATUS_LABEL = {
    "completed_verified": "Completed - independently verified",
    "completed_unverified": "Completed - NOT independently verified",
    "partial": "Partially completed",
    "blocked": "Blocked - needs a human",
    "failed": "Failed",
}


def _timeline(events: list["Event"]) -> list[dict]:
    """Pair tool_call/tool_result events into rows."""
    rows, pending = [], {}
    for e in events:
        if e.type == "tool_call":
            pending[(e.agent, e.data["tool"])] = {"agent": e.agent, "step": e.step, "tool": e.data["tool"],
                                                  "args": e.data["args"], "risk": e.data["risk"],
                                                  "decision": e.data["decision"]}
        elif e.type == "approval_decision":
            row = pending.get((e.agent, e.data["tool"]))
            if row:
                row["approved"] = e.data["approved"]
        elif e.type == "tool_result":
            row = pending.pop((e.agent, e.data["tool"]), {"agent": e.agent, "step": e.step, "tool": e.data["tool"],
                                                          "args": {}, "risk": "", "decision": ""})
            row.update(is_error=e.data["is_error"], text=e.data.get("text", ""), evidence=e.data.get("evidence", []))
            rows.append(row)
        elif e.type == "guard_notice":
            rows.append({"agent": e.agent, "step": e.step, "tool": "harness notice", "args": {}, "risk": "",
                         "decision": "", "is_error": False, "text": e.data["message"], "evidence": []})
    return rows


def write_reports(run_dir: Path, task: str, result: "TaskResult", events: list["Event"], plan: list[dict]) -> Path:
    rows = _timeline(events)
    (run_dir / "report.md").write_text(_markdown(task, result, rows, plan), encoding="utf-8")
    path = run_dir / "report.html"
    path.write_text(_html(task, result, rows, plan), encoding="utf-8")
    return path


def _markdown(task: str, r: "TaskResult", rows: list[dict], plan: list[dict]) -> str:
    out = [f"# Run report\n", f"**Task:** {task}\n", f"**Status:** {STATUS_LABEL.get(r.status, r.status)}\n",
           f"**Summary:** {r.summary}\n"]
    if r.finish and r.finish.get("result"):
        out.append("## Result\n")
        out += [f"- **{k}**: {v}" for k, v in r.finish["result"].items()]
        out.append("")
    for i, v in enumerate(r.verification, 1):
        out.append(f"## Verification round {i}: {v['verdict']}\n\n{v.get('summary', '')}\n")
        out += [f"- [{'x' if c['passed'] else ' '}] {c['claim']} - _{c['observed']}_" for c in v.get("checks", [])]
        out.append("")
    if plan:
        out.append("## Final plan\n")
        out += [f"- [{s['status']}] {s['title']}" for s in plan]
        out.append("")
    if r.memory:
        out.append("## Facts recorded\n")
        out += [f"- **{k}** = {v['value']} _(source: {v['source']})_" for k, v in r.memory.items()]
        out.append("")
    out.append("## Actions\n")
    for row in rows:
        flag = "ERROR " if row.get("is_error") else ""
        args = json.dumps(row["args"], ensure_ascii=False)
        out.append(f"- `{row['agent']}#{row['step']}` **{row['tool']}** {args} {flag}[{row['risk']}/{row['decision']}]")
    u = r.usage
    out.append(f"\n## Usage\n\n{u.get('llm_calls', 0)} model calls, {u.get('input_tokens', 0):,} input / "
               f"{u.get('output_tokens', 0):,} output tokens, {u.get('cache_read_input_tokens', 0):,} cache-read, "
               f"est. ${u.get('cost_usd', 0):.3f}\n")
    return "\n".join(out)


def _html(task: str, r: "TaskResult", rows: list[dict], plan: list[dict]) -> str:
    ok = r.status == "completed_verified"
    warn = r.status in ("completed_unverified", "partial", "blocked")
    tone = "ok" if ok else "warn" if warn else "bad"

    result_html = ""
    if r.finish and r.finish.get("result"):
        result_html = "<h2>Result</h2><dl class='kv'>" + "".join(
            f"<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>" for k, v in r.finish["result"].items()) + "</dl>"

    verify_html = ""
    for i, v in enumerate(r.verification, 1):
        checks = "".join(
            f"<tr><td class='{'pass' if c['passed'] else 'fail'}'>{'PASS' if c['passed'] else 'FAIL'}</td>"
            f"<td>{escape(c['claim'])}</td><td>{escape(c['observed'])}</td></tr>" for c in v.get("checks", []))
        verify_html += (f"<h2>Verification round {i}: {escape(v['verdict'])}</h2><p>{escape(v.get('summary', ''))}</p>"
                        f"<table><tr><th></th><th>Check</th><th>Observed</th></tr>{checks}</table>")

    evidence_html = ""
    if r.finish and r.finish.get("evidence"):
        evidence_html = "<h2>Worker's evidence</h2><ul>" + "".join(
            f"<li>{escape(e.get('claim', ''))} <span class='muted'>&mdash; {escape(e.get('source', ''))}</span></li>"
            for e in r.finish["evidence"]) + "</ul>"

    plan_html = ""
    if plan:
        plan_html = "<h2>Plan</h2><ol class='plan'>" + "".join(
            f"<li class='{escape(s['status'])}'><span class='tag'>{escape(s['status'])}</span> {escape(s['title'])}</li>"
            for s in plan) + "</ol>"

    memory_html = ""
    if r.memory:
        memory_html = "<h2>Facts recorded</h2><table><tr><th>Key</th><th>Value</th><th>Source</th></tr>" + "".join(
            f"<tr><td>{escape(k)}</td><td>{escape(v['value'])}</td><td class='muted'>{escape(v['source'])}</td></tr>"
            for k, v in r.memory.items()) + "</table>"

    steps = []
    for row in rows:
        shots = "".join(f"<a href='{escape(p)}' target='_blank'><img src='{escape(p)}' loading='lazy'></a>"
                        for p in row.get("evidence", []))
        badge = ""
        if row["risk"]:
            badge = f"<span class='risk {escape(row['risk'])}'>{escape(row['risk'])}</span>"
        if row.get("approved") is not None:
            badge += f"<span class='risk {'read' if row['approved'] else 'external'}'>{'approved' if row['approved'] else 'declined'}</span>"
        steps.append(
            f"<details class='step{' err' if row.get('is_error') else ''}'><summary><span class='who'>{escape(row['agent'])} "
            f"#{row['step']}</span> <b>{escape(row['tool'])}</b> <code>{escape(json.dumps(row['args'], ensure_ascii=False)[:160])}</code> "
            f"{badge}</summary><pre>{escape(row.get('text', '')[:3000])}</pre>{shots}</details>")

    u = r.usage
    usage = (f"{u.get('llm_calls', 0)} model calls &middot; {u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out "
             f"&middot; {u.get('cache_read_input_tokens', 0):,} cached &middot; est. ${u.get('cost_usd', 0):.3f}")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Run report</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --fg:#1b2130; --muted:#667085; --line:#e4e7ec; --ok:#12805c; --warn:#b54708; --bad:#c01048; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1218; --card:#171b23; --fg:#e6e9ef; --muted:#98a2b3; --line:#2a303b; --ok:#3ccb7f; --warn:#f79009; --bad:#f63d68; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.55 system-ui,Segoe UI,sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:980px; margin:0 auto; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:24px; }}
h1 {{ margin:0 0 4px; font-size:20px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.status {{ display:inline-block; padding:4px 10px; border-radius:999px; font-weight:600; margin:8px 0; }}
.status.ok {{ color:var(--ok); border:1px solid var(--ok); }} .status.warn {{ color:var(--warn); border:1px solid var(--warn); }} .status.bad {{ color:var(--bad); border:1px solid var(--bad); }}
.muted {{ color:var(--muted); }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
td.pass {{ color:var(--ok); font-weight:600; }} td.fail {{ color:var(--bad); font-weight:600; }}
dl.kv {{ display:grid; grid-template-columns:180px 1fr; gap:4px 12px; }} dl.kv dt {{ color:var(--muted); }}
.plan li {{ margin:2px 0; }} .tag {{ font-size:11px; padding:1px 6px; border-radius:4px; border:1px solid var(--line); color:var(--muted); }}
details.step {{ border:1px solid var(--line); border-radius:6px; margin:6px 0; padding:6px 10px; }} details.step.err {{ border-color:var(--bad); }}
details.step summary {{ cursor:pointer; overflow-wrap:anywhere; }} .who {{ color:var(--muted); font-size:12px; }}
code {{ font-size:12px; color:var(--muted); }} pre {{ white-space:pre-wrap; font-size:12px; max-height:360px; overflow:auto; background:var(--bg); padding:8px; border-radius:4px; }}
details img {{ max-width:100%; border:1px solid var(--line); border-radius:4px; margin-top:6px; }}
.risk {{ font-size:11px; padding:1px 6px; border-radius:4px; margin-left:4px; border:1px solid currentColor; }}
.risk.read {{ color:var(--ok); }} .risk.write {{ color:var(--warn); }} .risk.external {{ color:var(--bad); }}
</style></head><body><main>
<h1>Run report</h1>
<div class="muted">{escape(task)}</div>
<div class="status {tone}">{escape(STATUS_LABEL.get(r.status, r.status))}</div>
<p>{escape(r.summary).replace(chr(10), '<br>')}</p>
{result_html}{verify_html}{evidence_html}{plan_html}{memory_html}
<h2>Step-by-step trace</h2>{''.join(steps)}
<p class="muted" style="margin-top:20px">{usage}</p>
</main></body></html>"""
