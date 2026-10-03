"""Browser capability built on Playwright.

Observation model: instead of raw HTML or pixels, the model gets a compact
text rendering of the page in reading order where every interactive element
carries a ref (`[e7 button "Save bill"]`). Actions address elements by ref.
This works on any reasonably semantic web app without per-site code, keeps
token use low, and makes actions unambiguous. Screenshots are still captured
after every action as evidence, and the model can request one explicitly
when layout matters.

Every action returns the fresh page state, so "act" and "observe" are one
round trip and the model always reasons about the real outcome.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from ..policy import Risk, classify_control
from .base import Tool, ToolError, ToolResult

if TYPE_CHECKING:
    from ..runtime import RunState, ToolContext

SNAPSHOT_JS = r"""
() => {
  const MAX_OPTIONS = 25;
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  let n = 0, cellDepth = 0;
  const lines = [];
  let cur = [];
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const q = s => '"' + norm(s).slice(0, 100).replace(/"/g, "'") + '"';
  const flush = () => { if (cellDepth) return; const s = norm(cur.join(' ')); if (s) lines.push(s); cur = []; };
  const visible = el => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || st.display === 'contents';
  };
  const labelOf = el => {
    if (el.labels && el.labels.length) return norm(el.labels[0].innerText);
    return norm(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.name || '');
  };
  const mark = el => { const r = 'e' + (++n); el.setAttribute('data-agent-ref', r); return r; };
  const BLOCK = new Set(['P','DIV','SECTION','ARTICLE','MAIN','HEADER','FOOTER','NAV','UL','OL','LI','TABLE','THEAD','TBODY',
                         'FORM','DL','BLOCKQUOTE','FIELDSET','ASIDE','BR','HR','CAPTION']);
  const walk = node => {
    if (node.nodeType === 3) { const t = norm(node.textContent); if (t) cur.push(t); return; }
    if (node.nodeType !== 1) return;
    const el = node, tag = el.tagName;
    if (['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','svg','SVG','HEAD'].includes(tag)) return;
    if (tag === 'INPUT' && el.type === 'hidden') return;
    if (!visible(el)) return;
    if (tag === 'LABEL' && el.control) return;  // folded into the control it labels
    const role = el.getAttribute('role');
    if (/^H[1-6]$/.test(tag)) { flush(); cur.push('#'.repeat(+tag[1]) + ' ' + norm(el.innerText)); flush(); return; }
    if (tag === 'PRE') { flush(); el.innerText.split('\n').forEach(l => { if (norm(l)) lines.push(norm(l)); }); return; }
    if (tag === 'A' && el.hasAttribute('href')) {
      cur.push('[' + mark(el) + ' link ' + q(el.innerText || el.getAttribute('aria-label')) + ' -> ' + el.getAttribute('href') + ']'); return;
    }
    if (tag === 'BUTTON' || (tag === 'INPUT' && ['submit','button','reset'].includes(el.type)) || role === 'button') {
      cur.push('[' + mark(el) + ' button ' + q(el.innerText || el.value || el.getAttribute('aria-label')) + (el.disabled ? ' disabled' : '') + ']'); return;
    }
    if (tag === 'INPUT' && ['checkbox','radio'].includes(el.type)) {
      flush(); cur.push('[' + mark(el) + ' ' + el.type + ' ' + q(labelOf(el)) + (el.checked ? ' checked' : '') + ']'); flush(); return;
    }
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      const kind = el.type === 'password' ? 'password' : el.type === 'search' ? 'searchbox' : el.type === 'file' ? 'file' : 'textbox';
      const val = el.type === 'password' ? (el.value ? '********' : '') : el.value;
      let s = '[' + mark(el) + ' ' + kind + ' ' + q(labelOf(el)) + ' value=' + q(val);
      if (el.placeholder && el.placeholder !== labelOf(el)) s += ' placeholder=' + q(el.placeholder);
      flush(); cur.push(s + ']'); flush(); return;
    }
    if (tag === 'SELECT') {
      const opts = [...el.options].map(o => norm(o.text));
      const sel = el.selectedOptions[0] ? norm(el.selectedOptions[0].text) : '';
      flush();
      cur.push('[' + mark(el) + ' select ' + q(labelOf(el)) + ' selected=' + q(sel) + ' options=' + JSON.stringify(opts.slice(0, MAX_OPTIONS)) +
               (opts.length > MAX_OPTIONS ? ' (+' + (opts.length - MAX_OPTIONS) + ' more)' : '') + ']');
      flush(); return;
    }
    if (tag === 'TR') {
      flush();
      cellDepth++;
      const cells = [...el.children].map(c => { const saved = cur; cur = []; c.childNodes.forEach(walk); const s = norm(cur.join(' ')); cur = saved; return s; });
      cellDepth--;
      cur.push('| ' + cells.join(' | ') + ' |'); flush(); return;
    }
    if (tag === 'DT') { flush(); cur.push(norm(el.innerText) + ':'); return; }
    if (tag === 'DD') { el.childNodes.forEach(walk); flush(); return; }
    const block = BLOCK.has(tag) || tag === 'LI' || tag === 'TR';
    if (block) flush();
    if (role === 'alert' || role === 'status') cur.push('[' + role + ']');
    el.childNodes.forEach(walk);
    if (block || role === 'alert' || role === 'status') flush();
  };
  walk(document.body);
  flush();
  return { url: location.href, title: document.title, lines };
}
"""

CONTROL_INFO_JS = r"""
el => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const tag = el.tagName;
  const label = norm(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
  const form = el.form || el.closest('form');
  const isSubmit = (tag === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit') || (tag === 'INPUT' && el.type === 'submit');
  const isTextEntry = (tag === 'INPUT' && !['submit','button','checkbox','radio','hidden'].includes(el.type)) || tag === 'TEXTAREA';
  const fields = [];
  if (form) {
    for (const f of form.elements) {
      if (!f.name || ['hidden','submit','button'].includes(f.type) || f.tagName === 'BUTTON') continue;
      const v = f.type === 'password' ? '********' : f.tagName === 'SELECT' ? (f.selectedOptions[0] ? f.selectedOptions[0].text : '') : f.value;
      fields.push([(f.labels && f.labels[0] ? norm(f.labels[0].innerText) : f.name), String(v).slice(0, 300)]);
    }
  }
  return { tag, label, href: el.getAttribute('href'), isSubmit, isTextEntry,
           hasForm: !!form, formMethod: form ? (form.getAttribute('method') || 'get') : null,
           formAction: form ? form.getAttribute('action') : null, fields };
}
"""

SNAPSHOT_CHAR_LIMIT = 9000


class BrowserManager:
    """One browser + context per run; each agent gets its own tab sharing cookies."""

    def __init__(self, run: "RunState"):
        self.run = run
        self._pw = sync_playwright().start()
        headless = run.settings.headless
        self.browser = self._pw.chromium.launch(headless=headless, slow_mo=0 if headless else 250)
        self.context = self.browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
        self.tabs: dict[str, BrowserTab] = {}

    def tab(self, name: str) -> "BrowserTab":
        if name not in self.tabs:
            self.tabs[name] = BrowserTab(self, name)
        return self.tabs[name]

    def close(self) -> None:
        for closer in (self.context.close, self.browser.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass


class BrowserTab:
    def __init__(self, manager: BrowserManager, name: str):
        self.manager = manager
        self.name = name
        self.page = manager.context.new_page()
        self.page.set_default_timeout(8000)
        self.pending_downloads: list = []
        self.last_status: int | None = None
        self.shots = 0
        self.page.on("download", lambda download: self.pending_downloads.append(download))
        self.page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers

    def resolve_url(self, url: str) -> str:
        if not urlparse(url).scheme and self.page.url.startswith("http"):
            url = urljoin(self.page.url, url)
        return url

    def check_allowed(self, url: str) -> None:
        host = urlparse(url).netloc
        allowed = self.manager.run.profile.allowed_hosts
        if host not in allowed:
            raise ToolError(f"Blocked by policy: {host or url!r} is not an allowed host. Allowed: {', '.join(allowed)}")

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except PlaywrightTimeout:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=1500)
        except PlaywrightTimeout:
            pass
        self.page.wait_for_timeout(150)  # let download/response events dispatch

    def locate(self, ref: str):
        loc = self.page.locator(f'[data-agent-ref="{ref}"]')
        if loc.count() == 0:
            raise ToolError(f"Element {ref} is not on the current page (the page probably changed). "
                            "Use the refs from the most recent page state, or call browser_snapshot.")
        return loc.first

    def control_info(self, ref: str) -> dict | None:
        loc = self.page.locator(f'[data-agent-ref="{ref}"]')
        if loc.count() == 0:
            return None
        return loc.first.evaluate(CONTROL_INFO_JS)

    def collect_downloads(self, workspace: Path) -> list[str]:
        saved = []
        target_dir = workspace / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        while self.pending_downloads:
            download = self.pending_downloads.pop(0)
            path = target_dir / Path(download.suggested_filename).name
            download.save_as(path)
            saved.append(f"downloads/{path.name} ({path.stat().st_size / 1024:.1f} KB)")
        return saved

    def snapshot(self, find: str | None = None) -> str:
        if not self.page.url.startswith("http"):
            return "No page loaded yet. Use browser_navigate."
        data = self.page.evaluate(SNAPSHOT_JS)
        lines = data["lines"]
        header = f"URL: {data['url']}\nTitle: {data['title']}"
        if self.last_status and self.last_status >= 400:
            header += f"\nHTTP status: {self.last_status} (error)"
        if find:
            terms = [t.strip().lower() for t in find.split("|") if t.strip()]
            matched = [ln for ln in lines if any(t in ln.lower() for t in terms)]
            body = "\n".join(matched) if matched else f"(no lines match {find!r})"
            return f"{header}\nFiltered by {find!r}: {len(matched)} of {len(lines)} lines\n---\n{body}"
        body, used = [], 0
        for i, line in enumerate(lines):
            if used + len(line) > SNAPSHOT_CHAR_LIMIT:
                body.append(f"... [{len(lines) - i} more lines truncated; call browser_snapshot with find='text' to search]")
                break
            body.append(line)
            used += len(line) + 1
        return f"{header}\n---\n" + "\n".join(body)

    def screenshot(self, ctx: "ToolContext", label: str) -> tuple[str, bytes]:
        self.shots += 1
        rel = f"screenshots/{ctx.agent}-s{ctx.step:02d}-{self.shots:02d}-{label}.png"
        png = self.page.screenshot(full_page=False)
        (ctx.run.run_dir / rel).write_bytes(png)
        return rel, png

    def after_action(self, ctx: "ToolContext", summary: str, label: str) -> ToolResult:
        """Shared post-action observation: downloads, status, fresh snapshot, evidence."""
        self.settle()
        downloads = self.collect_downloads(ctx.run.workspace)
        if self.last_status and self.last_status >= 400:
            summary += f" -> HTTP {self.last_status}"
        parts = [summary]
        if self.last_status and self.last_status >= 500:
            parts.append(f"WARNING: the server returned HTTP {self.last_status}. The outcome is unknown - the action may "
                         "or may not have taken effect. Check the actual state before retrying anything that creates or "
                         "sends data.")
        elif self.last_status and self.last_status >= 400:
            parts.append(f"The server rejected the request (HTTP {self.last_status}). The page below should say why.")
        if downloads:
            parts.append("Downloaded to workspace: " + ", ".join(downloads) + " (use read_file to read)")
        parts.append(self.snapshot())
        rel, _ = self.screenshot(ctx, label)
        return ToolResult("\n".join(parts), evidence=[rel], meta={"url": self.page.url, "status": self.last_status})


# ---------------------------------------------------------------------- tools


class BrowserNavigate(Tool):
    name = "browser_navigate"
    description = ("Open a URL in the browser (absolute, or relative to the current page). "
                   "Returns the resulting page state with element refs.")
    input_schema = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "URL to open"}},
        "required": ["url"],
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        tab = ctx.tab
        url = tab.resolve_url(args["url"])
        tab.check_allowed(url)
        tab.last_status = None
        last_error = None
        # Navigation is idempotent, so transient failures are retried here
        # instead of spending model turns on them.
        for attempt in range(3):
            try:
                tab.page.goto(url, wait_until="domcontentloaded")
                break
            except PlaywrightError as e:
                msg = str(e)
                if "Download is starting" in msg or "ERR_ABORTED" in msg:
                    break  # a file download, collected in after_action
                last_error = msg
                time.sleep(0.5 * (2 ** attempt))
        else:
            raise ToolError(f"Could not load {url} after 3 attempts: {last_error.splitlines()[0] if last_error else ''}")
        return tab.after_action(ctx, f"Navigated to {url}.", "navigate")


class BrowserSnapshot(Tool):
    name = "browser_snapshot"
    description = ("Re-read the current page. Optional `find` filters to lines containing any of the given "
                   "'|'-separated terms (useful on long pages).")
    input_schema = {
        "type": "object",
        "properties": {"find": {"type": "string", "description": "e.g. 'invoice|total'"}},
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        return ToolResult(ctx.tab.snapshot(args.get("find")))


class BrowserClick(Tool):
    name = "browser_click"
    description = ("Click an element by ref (links, buttons, checkboxes). Clicking a submit button submits its form. "
                   "Returns the resulting page state; file downloads are saved to the workspace automatically.")
    input_schema = {
        "type": "object",
        "properties": {"ref": {"type": "string", "description": "Element ref from the latest page state, e.g. 'e7'"}},
        "required": ["ref"],
        "additionalProperties": False,
    }

    def classify(self, args: dict, ctx: "ToolContext") -> Risk:
        info = ctx.tab.control_info(args["ref"])
        if not info:
            return Risk.READ  # run() will report the stale ref
        return classify_control(info["label"], info["isSubmit"] and info["hasForm"], info["formMethod"])

    def describe_action(self, args: dict, ctx: "ToolContext") -> str:
        info = ctx.tab.control_info(args["ref"]) or {}
        text = f'Click "{info.get("label", args["ref"])}" on "{ctx.tab.page.title()}" ({ctx.tab.page.url})'
        if info.get("isSubmit") and info.get("fields"):
            text += "\nForm contents:\n" + "\n".join(f"  {k}: {v}" for k, v in info["fields"])
        return text

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        tab = ctx.tab
        loc = tab.locate(args["ref"])
        info = loc.evaluate(CONTROL_INFO_JS)
        if info.get("href") and not info["href"].startswith(("#", "javascript:")):
            tab.check_allowed(tab.resolve_url(info["href"]))
        tab.last_status = None
        try:
            loc.click(timeout=5000)
        except PlaywrightTimeout:
            raise ToolError(f"Element {args['ref']} could not be clicked (hidden, disabled or covered).")
        return tab.after_action(ctx, f'Clicked {args["ref"]} ("{info.get("label", "")[:60]}").', "click")


FIELD_KIND_JS = r"""
el => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const tag = el.tagName;
  const label = el.labels && el.labels.length ? norm(el.labels[0].innerText) : norm(el.getAttribute('aria-label') || el.name || '');
  if (tag === 'SELECT') return { kind: 'select', tag: 'dropdown', label, options: [...el.options].map(o => norm(o.text)) };
  if (tag === 'TEXTAREA') return { kind: 'text', tag: 'text area', label };
  if (tag === 'INPUT') {
    if (['checkbox', 'radio'].includes(el.type)) return { kind: el.type, tag: el.type, label };
    if (['submit', 'button', 'reset', 'hidden', 'file', 'image'].includes(el.type)) return { kind: 'other', tag: el.type + ' button', label };
    return { kind: 'text', tag: 'text field', label };
  }
  if (el.isContentEditable) return { kind: 'text', tag: 'editable region', label };
  return { kind: 'other', tag: tag.toLowerCase(), label };
}
"""

TRUE_WORDS = {"true", "yes", "on", "1", "checked", "check"}


class BrowserFill(Tool):
    name = "browser_fill"
    description = ("Fill form fields by ref in one action: text fields take text, dropdowns take the visible option "
                   "label, checkboxes take true/false. Fill all the fields of a form in a single call. Use "
                   "{{secret:NAME}} for credentials - the harness fills in the value and you never see it. To submit, "
                   "pass submit_ref (the button to click) or press_enter=true; the resulting page is returned.")
    input_schema = {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"ref": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["ref", "value"],
                },
            },
            "submit_ref": {"type": "string", "description": "Button to click after filling, e.g. Save or Sign in"},
            "press_enter": {"type": "boolean", "description": "Press Enter in the last field instead of clicking a button"},
        },
        "required": ["fields"],
        "additionalProperties": False,
    }

    def classify(self, args: dict, ctx: "ToolContext") -> Risk:
        if args.get("submit_ref"):
            return BrowserClick().classify({"ref": args["submit_ref"]}, ctx)
        if args.get("press_enter") and args["fields"]:
            info = ctx.tab.control_info(args["fields"][-1]["ref"])
            return classify_control("", bool(info and info["hasForm"]), info["formMethod"] if info else None)
        return Risk.READ

    def describe_action(self, args: dict, ctx: "ToolContext") -> str:
        planned: dict[str, str] = {}
        for f in args["fields"]:
            loc = ctx.tab.page.locator(f'[data-agent-ref="{f["ref"]}"]')
            label = (loc.first.evaluate(FIELD_KIND_JS)["label"] if loc.count() else "") or f["ref"]
            planned[label] = "********" if "{{secret:" in f["value"] else f["value"]
        info = ctx.tab.control_info(args["submit_ref"]) if args.get("submit_ref") else None
        action = f'Fill the form and click "{info["label"]}"' if info else "Fill the form and press Enter"
        text = f'{action} on "{ctx.tab.page.title()}" ({ctx.tab.page.url})'
        fields = [(k, planned.pop(k, v)) for k, v in (info or {}).get("fields", [])] + list(planned.items())
        if fields:
            text += "\nForm contents:\n" + "\n".join(f"  {k}: {v}" for k, v in fields)
        return text

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        tab = ctx.tab
        if not args["fields"] and not args.get("submit_ref"):
            raise ToolError("Nothing to do: pass fields and/or submit_ref.")
        done: list[str] = []

        def progress() -> str:
            return f"Fields filled before this error: {', '.join(done) or 'none'}."

        for f in args["fields"]:
            loc = tab.locate(f["ref"])
            kind = loc.evaluate(FIELD_KIND_JS)
            try:
                value = ctx.run.vault.resolve(f["value"])
            except KeyError as e:
                raise ToolError(f"{e.args[0]} {progress()}")
            if kind["kind"] == "select":
                wanted = value.strip()
                match = next((o for o in kind["options"] if o == wanted), None) or next(
                    (o for o in kind["options"] if o.lower() == wanted.lower()), None)
                if match is None:
                    raise ToolError(f"No option {wanted!r} in {f['ref']} ({kind['label']}). Options: {kind['options']}. "
                                    + progress())
                loc.select_option(label=match)
                shown = match
            elif kind["kind"] == "text":
                loc.fill(value)
                shown = "********" if "{{secret:" in f["value"] else value
            elif kind["kind"] in ("checkbox", "radio"):
                checked = value.strip().lower() in TRUE_WORDS
                loc.set_checked(checked)
                shown = "checked" if checked else "unchecked"
            else:
                raise ToolError(f"{f['ref']} is a {kind['tag']}, which cannot be filled - use browser_click. "
                                + progress())
            done.append(f'{kind["label"] or f["ref"]}="{shown[:60]}"')

        summary = f"Filled {len(done)} field(s): {', '.join(done)}" if done else "No fields filled"
        if args.get("submit_ref"):
            button = tab.locate(args["submit_ref"])
            info = button.evaluate(CONTROL_INFO_JS)
            tab.last_status = None
            try:
                button.click(timeout=5000)
            except PlaywrightTimeout:
                raise ToolError(f"{args['submit_ref']} could not be clicked (hidden, disabled or covered). {progress()}")
            return tab.after_action(ctx, f'{summary}; clicked {args["submit_ref"]} ("{info["label"][:40]}").', "submit")
        if args.get("press_enter") and args["fields"]:
            tab.last_status = None
            tab.locate(args["fields"][-1]["ref"]).press("Enter")
            return tab.after_action(ctx, f"{summary}; pressed Enter.", "submit")
        return ToolResult(summary + ".")


class BrowserScreenshot(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot of the current page and look at it. Use when layout or visual state matters."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        if not ctx.tab.page.url.startswith("http"):
            raise ToolError("No page loaded yet.")
        rel, png = ctx.tab.screenshot(ctx, "screenshot")
        return ToolResult(f"Screenshot of {ctx.tab.page.url}", images=[png], evidence=[rel])


BROWSER_TOOLS = [BrowserNavigate(), BrowserSnapshot(), BrowserClick(), BrowserFill(), BrowserScreenshot()]
