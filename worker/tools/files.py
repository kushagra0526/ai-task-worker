"""Sandboxed workspace file access (downloads land here too)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pypdf import PdfReader

from ..policy import Risk
from .base import Tool, ToolError, ToolResult

if TYPE_CHECKING:
    from ..runtime import ToolContext

READ_LIMIT = 20_000


def _resolve(ctx: "ToolContext", rel: str) -> Path:
    root = ctx.run.workspace.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise ToolError(f"Path {rel!r} is outside the workspace.")
    return path


class ListFiles(Tool):
    name = "list_files"
    description = "List files in the workspace (or a subdirectory of it)."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory relative to the workspace; default '.'"}},
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        base = _resolve(ctx, args.get("path", "."))
        if not base.is_dir():
            raise ToolError(f"{args.get('path')!r} is not a directory.")
        root = ctx.run.workspace.resolve()
        entries = sorted(p for p in base.rglob("*") if p.is_file())
        if not entries:
            return ToolResult("(empty)")
        return ToolResult("\n".join(f"{p.relative_to(root).as_posix()}  ({p.stat().st_size} bytes)" for p in entries[:200]))


class ReadFile(Tool):
    name = "read_file"
    description = "Read a workspace file. Text, CSV, JSON and Markdown are returned as-is; PDFs are converted to text."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path relative to the workspace"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        path = _resolve(ctx, args["path"])
        if not path.is_file():
            raise ToolError(f"No such file: {args['path']!r}. Use list_files to see what exists.")
        if path.suffix.lower() == ".pdf":
            try:
                reader = PdfReader(str(path))
                text = "\n\n".join(f"[page {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages))
            except Exception as e:
                raise ToolError(f"Could not parse PDF: {e}")
            if not text.strip():
                raise ToolError("The PDF has no extractable text (it may be a scanned image).")
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > READ_LIMIT:
            text = text[:READ_LIMIT] + f"\n... [truncated, {len(text) - READ_LIMIT} more characters]"
        return ToolResult(text)


class WriteFile(Tool):
    name = "write_file"
    description = "Create a file in the workspace (e.g. a CSV export or a note). Refuses to overwrite unless overwrite=true."
    risk = Risk.WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        path = _resolve(ctx, args["path"])
        if path.exists() and not args.get("overwrite"):
            raise ToolError(f"{args['path']!r} already exists. Pass overwrite=true to replace it.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return ToolResult(f"Wrote {len(args['content'])} characters to {args['path']}.", meta={"applied": True})


FILE_TOOLS = [ListFiles(), ReadFile(), WriteFile()]
