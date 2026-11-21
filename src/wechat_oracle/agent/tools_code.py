# -*- coding: utf-8 -*-
"""Agent tool: compile and run short C / C++ / Python programs in a sandbox.

Mirrors the conventions of the sibling modules (`tools_read.py`,
`tools_write.py`): a module-level `ToolSpec`, one `Tool` subclass whose `call`
returns the text the model will see, and a `register_*_tools(tools)` helper the
orchestrator calls once per run.

Nothing here is group-scoped — the sandbox has no access to the message DB and
no way to reach another group's data, so the tool takes no `conn`/`group_id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import sandbox
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm


_RUN_CODE_SPEC = ToolSpec(
    name="run_code",
    description=(
        "Compile and run a short C, C++ or Python program in an isolated "
        "sandbox and return its output. Use this to check code you are about "
        "to give someone: write it, run it, read the compiler or runtime "
        "errors, then fix and call again. The sandbox has NO network and no "
        "access to this machine's files, so only what you pass in exists. "
        "Default limit is 15 seconds of runtime."
    ),
    parameters={
        "type": "object",
        "properties": {
            "language": {
                "type": "string",
                "enum": ["cpp", "c", "python"],
                "description": "Which compiler/interpreter to use.",
            },
            "code": {
                "type": "string",
                "description": (
                    "Full source of the entry file. For C/C++ this is the "
                    "translation unit that contains main(); for Python the "
                    "module that is executed. Include headers/imports."
                ),
            },
            "extra_files": {
                "type": "object",
                "description": (
                    "Optional additional files as {filename: content}. Useful "
                    "for a header, a second .cpp, or a data file. The entry "
                    "file name is reserved and will be ignored if repeated."
                ),
                "additionalProperties": {"type": "string"},
            },
            "stdin": {
                "type": "string",
                "description": "Text piped to the program's standard input.",
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 1,
                "maximum": 60,
                "description": "Wall-clock limit in seconds. Defaults to 15.",
            },
        },
        "required": ["language", "code"],
    },
)


@dataclass
class RunCodeTool(Tool):
    spec = _RUN_CODE_SPEC

    def call(self, args: dict[str, Any]) -> str:
        language = args.get("language")
        if not isinstance(language, str) or not language.strip():
            raise ToolError("language must be one of: cpp, c, python")
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ToolError("code must be a non-empty string")

        extra = args.get("extra_files")
        if extra is not None and not isinstance(extra, dict):
            raise ToolError("extra_files must be an object of {filename: content}")
        extra = {str(k): str(v) for k, v in (extra or {}).items()}

        stdin = args.get("stdin") or ""
        if not isinstance(stdin, str):
            raise ToolError("stdin must be a string")

        timeout_s = args.get("timeout_s", sandbox.DEFAULT_TIMEOUT_S)
        try:
            timeout_s = int(timeout_s)
        except (TypeError, ValueError):
            timeout_s = sandbox.DEFAULT_TIMEOUT_S

        try:
            result = sandbox.run(language, code, extra_files=extra,
                                 stdin=stdin, timeout_s=timeout_s)
        except sandbox.SandboxError as exc:
            # The model cannot fix a missing image or a stopped daemon, so this
            # is reported plainly rather than dressed up as a code error.
            raise ToolError("sandbox unavailable: %s" % exc)

        return truncate_for_llm(result, limit=4000)


def register_code_tools(tools) -> None:
    """Register the code-execution tool. Called once per agent run."""
    tools.register(RunCodeTool())
