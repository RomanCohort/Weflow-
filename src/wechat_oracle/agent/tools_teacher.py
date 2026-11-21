# -*- coding: utf-8 -*-
"""Agent tool: ask DeepSeek (the teacher) for advice when stuck.

This is deliberately a last resort, not a general-purpose delegation. The
tool description tells the model to use it only when it cannot make progress
on its own. Backend: https://www.romancohort.cloud/api/askteacher, which reads
the DeepSeek credentials on the server and never exposes them.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from .tools import Tool, ToolError, ToolSpec, truncate_for_llm

API = "https://www.romancohort.cloud/api/askteacher"
TIMEOUT = 240
MAX_QUESTION = 2000
RESULT_CHARS = 4000


_TEACHER_SPEC = ToolSpec(
    name="ask_teacher",
    description=(
        "Ask DeepSeek-Flash (the teacher, who lives on the server) for advice "
        "when you are genuinely stuck. Use this ONLY as a last resort: when you "
        "have tried and failed, when a bug or decision is beyond you, or when "
        "you need a second opinion you cannot form yourself. Describe the "
        "problem precisely — what you tried, what happened — not just 'help "
        "me'. Do NOT use it for ordinary questions you can answer, and do not "
        "use it just to avoid thinking. The teacher's answer is advice: verify "
        "it before acting on it. If you call this tool, you MUST tell the user "
        "in your reply that you asked the teacher — naturally, in your own "
        "voice (e.g. '我问了下老师'), not as a stiff citation. Never pass the "
        "teacher's words off as your own."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "A precise description of the difficulty: what you are "
                    "trying to do, what you tried, and where it broke."
                ),
                "maxLength": MAX_QUESTION,
            },
            "context": {
                "type": "string",
                "description": "Optional extra detail: error text, code, or constraints.",
            },
        },
        "required": ["question"],
    },
)


@dataclass
class AskTeacherTool(Tool):
    spec = _TEACHER_SPEC

    def call(self, args: dict[str, Any]) -> str:
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ToolError("question must be a non-empty string")
        question = question.strip()[:MAX_QUESTION]
        if len(question) < 4:
            raise ToolError("question is too short; describe the difficulty")

        extra = args.get("context")
        if isinstance(extra, str) and extra.strip():
            question = question + "\n\n补充信息：\n" + extra.strip()[:MAX_QUESTION]

        body = json.dumps({"question": question}).encode("utf-8")
        req = urllib.request.Request(
            API, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "wechat-oracle/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.load(r)
        except Exception as exc:
            raise ToolError("teacher unavailable: %s: %s"
                            % (type(exc).__name__, str(exc)[:140]))

        if data.get("error"):
            raise ToolError("teacher error: %s" % str(data["error"])[:200])
        answer = str(data.get("answer") or "").strip()
        if not answer:
            raise ToolError("teacher returned an empty answer; try again or proceed alone")
        model = str(data.get("model") or "deepseek-flash")
        # This text goes straight into the model's context as the tool result.
        # Putting the disclosure rule here (not only in the spec) is what
        # actually makes it survive: the model reads it at the moment it has
        # to compose the reply.
        out = (
            "老师的建议（%s，仅供参考，请自行核实）：\n\n%s\n\n"
            "[给 Occamy 的硬性要求：既然问了老师，回复用户时必须自然地说出"
            "「我问了下老师」这层意思，用自己的语气讲，不要生硬引用，"
            "更不要把老师的建议说成是自己想出来的。]"
        ) % (model, answer)
        return truncate_for_llm(out, limit=RESULT_CHARS)


def register_teacher_tools(tools) -> None:
    tools.register(AskTeacherTool())
