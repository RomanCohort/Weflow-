"""Group-wide knowledge retrieval for every chat — no classroom session needed.

The classroom path (`agent/knowledge_tools.py`) binds retrieval to a
CourseSession and records receipts. Ordinary groups only need read-only
search over the same index: historical messages, published notices, and the
curated `kind='note'` documents written by `write_note` / the memory archive.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..knowledge_views import find
from .tools import Tool, ToolError, ToolSpec

_SEARCH_SPEC = ToolSpec(
    name="search_knowledge",
    description=(
        "按语义检索本群知识库：历史消息、已确认的公告、以及归档笔记"
        "（kind=note，包括被压缩掉的旧记忆）。适合查「以前谁说过什么」"
        "「某件事的结论是什么」「群里的长期约定」。结果只是来源片段，"
        "不等于当前状态；命中某条消息也不代表存在日程或待办。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        "required": ["query"],
    },
)


@dataclass
class GroupKnowledgeSearchTool(Tool):
    spec = _SEARCH_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        try:
            limit = int(args.get("limit") or 6)
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(limit, 12))
        try:
            return find(self.conn, self.group_id, query, limit)
        except (ValueError, TypeError) as exc:
            raise ToolError(str(exc)) from exc


def register_group_knowledge(tools: Any) -> None:
    """Idempotent: classroom runs register their own search_knowledge first."""
    if tools.get("search_knowledge") is not None:
        return
    tools.register(GroupKnowledgeSearchTool(conn=tools.conn, group_id=tools.group_id))
