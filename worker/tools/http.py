"""HTTP/JSON API capability, restricted to the environment's allowed hosts."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from ..policy import Risk, classify_http
from .base import Tool, ToolError, ToolResult

if TYPE_CHECKING:
    from ..runtime import ToolContext

BODY_LIMIT = 12_000


class HttpRequest(Tool):
    name = "http_request"
    description = ("Call an HTTP API on an allowed host. Use {{secret:NAME}} in header values for credentials. "
                   "GET requests are retried automatically on transient errors; other methods are never auto-retried, "
                   "so check state before repeating one.")
    input_schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "url": {"type": "string"},
            "headers": {"type": "object", "description": "Header name -> value"},
            "json_body": {"type": "object", "description": "JSON request body"},
        },
        "required": ["method", "url"],
        "additionalProperties": False,
    }

    def classify(self, args: dict, ctx: "ToolContext") -> Risk:
        return classify_http(args.get("method", "GET"))

    def fingerprint(self, args: dict, ctx: "ToolContext") -> str:
        return f"{args['method'].upper()} {args['url']} {json.dumps(args.get('json_body'), sort_keys=True)}"

    def origin(self, args: dict, ctx: "ToolContext") -> str:
        return f"{args['method'].upper()} {args['url']}"

    def describe_action(self, args: dict, ctx: "ToolContext") -> str:
        body = json.dumps(args.get("json_body"), indent=2) if args.get("json_body") else ""
        return f"{args['method']} {args['url']}" + (f"\nBody:\n{body}" if body else "")

    def run(self, args: dict, ctx: "ToolContext") -> ToolResult:
        method, url = args["method"].upper(), args["url"]
        host = urlparse(url).netloc
        if host not in ctx.run.profile.allowed_hosts:
            raise ToolError(f"Blocked by policy: {host or url!r} is not an allowed host.")
        try:
            headers = {k: ctx.run.vault.resolve(str(v)) for k, v in (args.get("headers") or {}).items()}
        except KeyError as e:
            raise ToolError(str(e.args[0]))

        attempts = 3 if method == "GET" else 1
        last_error = ""
        for attempt in range(attempts):
            try:
                resp = httpx.request(method, url, headers=headers, json=args.get("json_body"), timeout=15)
                if resp.status_code >= 500 and attempt < attempts - 1:
                    last_error = f"HTTP {resp.status_code}"
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                break
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))
        else:
            if method != "GET":  # the request may have reached the server: the outcome is unknown, not "failed"
                return ToolResult(f"{method} {url} got no response ({last_error}). The outcome is unknown - the change "
                                  "may or may not have been applied. Check the current state before retrying.",
                                  is_error=True, meta={"attempted": True})
            raise ToolError(f"{method} {url} failed after {attempts} attempt(s): {last_error}")

        body = resp.text
        try:
            body = json.dumps(resp.json(), indent=2)
        except ValueError:
            pass
        if len(body) > BODY_LIMIT:
            body = body[:BODY_LIMIT] + "\n... [truncated]"
        text = f"HTTP {resp.status_code}\n{body}"
        if method != "GET" and resp.status_code >= 500:
            text = (f"WARNING: HTTP {resp.status_code} on a {method}. The outcome is unknown - the change may or may not "
                    f"have been applied. Check the current state before retrying.\n{text}")
        return ToolResult(text, is_error=resp.status_code >= 400, meta={"status": resp.status_code})


HTTP_TOOLS = [HttpRequest()]
