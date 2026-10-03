from .base import Tool, ToolError, ToolResult, validate_args
from .browser import BROWSER_TOOLS
from .control import AskUser, Finish, Remember, SubmitVerdict, UpdatePlan
from .files import FILE_TOOLS, ListFiles, ReadFile
from .http import HTTP_TOOLS


def worker_tools() -> list[Tool]:
    return [UpdatePlan(), Remember(), AskUser(), *BROWSER_TOOLS, *FILE_TOOLS, *HTTP_TOOLS, Finish()]


def verifier_tools() -> list[Tool]:
    # Same observation tools; the verifier runs under the read_only policy, so
    # anything classified WRITE/EXTERNAL is refused by the gate, not by omission.
    return [*BROWSER_TOOLS, ListFiles(), ReadFile(), *HTTP_TOOLS, SubmitVerdict()]


__all__ = ["Tool", "ToolError", "ToolResult", "validate_args", "worker_tools", "verifier_tools"]
