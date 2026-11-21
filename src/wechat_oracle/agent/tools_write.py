"""Phase B (write-only) tool implementations + the read pair needed for
replace-on-write semantics.

Two writers, both replace-on-write per group:
  - update_persona_drift(drift_text)  — bot's own behavior supplement
  - update_group_memory(notes_text)   — single freeform doc covering members,
                                        events, and group culture; bounded
                                        by WO_AGENT_MEMORY_MAX_CHARS

Why one blob instead of per-member rows: per-id modeling adds structure the
agent doesn't actually need. A single freeform document the agent organizes
internally is cheaper to read/write and easier to audit by hand. The hard
size cap forces compaction once full — agent must summarize old material
before adding new.

Normal Phase B does NOT get search_group_messages / vision / ASR — the trace
already digests what was learned in Phase A; reflection should be cheap.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import prompts
from ..config import settings
from ..log_utils import append_event
from .memory import (
    get_group_memory,
    get_persona_drift,
    upsert_group_memory,
    upsert_persona_drift,
)
from .tools import Tool, ToolError, ToolSpec


# --- read pair (Phase B uses these to read-before-write) ------------------


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class MemoryWriteSession:
    """Per-agent-run snapshots used to prevent replace-on-write lost updates."""

    persona_hash: str | None = None
    group_memory_hash: str | None = None


_READ_PERSONA_DRIFT_SPEC = ToolSpec(
    name="read_persona_drift",
    description=(
        "Read this group's evolvable persona supplement (drift). The static "
        "persona core lives elsewhere; what you can edit is just this "
        "addendum. Call before `update_persona_drift` so you replace with a "
        "merged version, not blow away history."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ReadPersonaDriftTool(Tool):
    spec = _READ_PERSONA_DRIFT_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = get_persona_drift(self.conn, self.group_id)
        self.session.persona_hash = _text_hash(text)
        return text or "(no drift recorded yet)"


_READ_GROUP_MEMORY_FOR_WRITE_SPEC = ToolSpec(
    name="read_group_memory",
    description=(
        "Read this group's freeform memory document. Call before "
        "`update_group_memory` so you replace with a merged version rather "
        "than overwriting blindly."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ReadGroupMemoryForWriteTool(Tool):
    """Same as the Phase A read tool but lives here so Phase B's tool
    registry is self-contained. Same return semantics."""
    spec = _READ_GROUP_MEMORY_FOR_WRITE_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = get_group_memory(self.conn, self.group_id)
        self.session.group_memory_hash = _text_hash(text)
        if not text:
            return "(empty — nothing learned about this group yet)"
        return text


# --- update_group_memory ---------------------------------------------------


_UPDATE_GROUP_MEMORY_SPEC = ToolSpec(
    name="update_group_memory",
    description=(
        "Replace this group's short working memory. It has exactly two "
        "sections and both are size-budgeted:\n"
        "  ## 固定  — 身份、群规、成员长期不变的事实（谁是老师/管理/父亲，"
        "群的固定约定）。预算约 900 字。\n"
        "  ## 近期  — 最近发生了什么、未收尾的事、新出现的梗。预算约 1800 字。\n"
        "两段合计不得超过 2700 字。写之前必须先用 read_group_memory 读现状，"
        "整段替换语义，不读就写等于丢历史。\n"
        "超出预算时先压缩：流水（谁几点说了什么）从「近期」删掉；"
        "长期有效的结论不要塞进来，用 write_note 写进知识库。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "notes_text": {
                "type": "string",
                "description": "Full new document text. Replaces what's there. Empty string = wipe (use sparingly).",
            },
        },
        "required": ["notes_text"],
    },
)


@dataclass
class UpdateGroupMemoryTool(Tool):
    spec = _UPDATE_GROUP_MEMORY_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = args.get("notes_text")
        if not isinstance(text, str):
            raise ToolError("notes_text must be a string")
        cap = settings.agent_memory_max_chars
        pinned_cap = settings.agent_memory_pinned_chars
        rolling_cap = settings.agent_memory_rolling_chars
        pinned, rolling = _split_memory_sections(text)
        if pinned is None:
            raise ToolError(
                "notes_text must contain the two sections '## 固定' and "
                "'## 近期' in that order. Move long-lived facts into 固定 and "
                "recent happenings into 近期."
            )
        if len(pinned) > pinned_cap:
            raise ToolError(
                f"「## 固定」section is {len(pinned)} chars; budget {pinned_cap}. "
                "Keep only identity / group rules / stable member facts there."
            )
        if len(rolling) > rolling_cap:
            raise ToolError(
                f"「## 近期」section is {len(rolling)} chars; budget {rolling_cap}. "
                "Drop finished business and chat-by-chat play; put durable "
                "conclusions into write_note instead."
            )
        if len(text) > cap:
            raise ToolError(
                f"notes_text too long ({len(text)} chars; max {cap}). "
                "Compact older / less relevant material before adding new."
            )
        previous = get_group_memory(self.conn, self.group_id)
        if self.session.group_memory_hash is None:
            raise ToolError(
                "read_group_memory must be called immediately before update_group_memory"
            )
        if _text_hash(previous) != self.session.group_memory_hash:
            # Invalidate the snapshot so a blind retry without re-read
            # cannot pass the check (and would clobber the concurrent write).
            # Forces the model to call read_group_memory again before retrying.
            self.session.group_memory_hash = None
            raise ToolError(
                "group_memory changed since you read it. Call read_group_memory again, "
                "merge your update with the current text, then retry."
            )
        upsert_group_memory(self.conn, self.group_id, text)
        self.session.group_memory_hash = _text_hash(text)
        append_event(
            "memory.write",
            group_id=self.group_id,
            table="group_memory",
            prev_len=len(previous),
            new_len=len(text),
        )
        return (
            f"group_memory updated. prev_len={len(previous)} new_len={len(text)} "
            f"(固定 {len(pinned)}/{pinned_cap}, 近期 {len(rolling)}/{rolling_cap})"
        )


def _split_memory_sections(text: str) -> tuple[str | None, str]:
    """Split '## 固定' / '## 近期'. Returns (None, "") when mis-structured."""
    marker = "## 固定"
    rolling_marker = "## 近期"
    if marker not in text or rolling_marker not in text:
        return None, ""
    if text.index(marker) > text.index(rolling_marker):
        return None, ""
    pinned = text.split(marker, 1)[1].split(rolling_marker, 1)[0].strip()
    rolling = text.split(rolling_marker, 1)[1].strip()
    return pinned, rolling


_WRITE_NOTE_SPEC = ToolSpec(
    name="write_note",
    description=(
        "把一条值得长期检索的结论写进本群知识库（kind=note），"
        "以后 search_knowledge 能按语义检索到。适合写：成员身份与角色、"
        "群规与长期约定、反复出现的梗、已经了结的事件的结论。\n"
        "不要写流水（谁几点说了什么），那属于 group_memory 的「近期」段。\n"
        "同一个 slug 再次写入是覆盖更新同一条笔记，不会重复建文档。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "一句话标题，检索时用于展示。"},
            "body": {"type": "string", "description": "结论正文，控制在 800 字以内。"},
            "slug": {
                "type": "string",
                "description": "稳定的短标识（英文/数字/连字符），同一主题用同一个，便于覆盖更新。",
            },
        },
        "required": ["title", "body"],
    },
)


def _note_slug(value: str, title: str) -> str:
    keep = "".join(c for c in value.lower() if c.isalnum() or c in "-_.")
    keep = keep.strip("-_.")[:40]
    if keep:
        return keep
    return hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]


@dataclass
class WriteNoteTool(Tool):
    spec = _WRITE_NOTE_SPEC
    conn: sqlite3.Connection
    group_id: str
    group_name: str = ""

    def call(self, args: dict[str, Any]) -> str:
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        if not title or not body:
            raise ToolError("title and body are both required")
        if len(body) > 1200:
            raise ToolError(
                f"body too long ({len(body)} chars; max 1200). Split it or trim."
            )
        slug = _note_slug(str(args.get("slug") or ""), title)
        from ..knowledge import new_document, revision
        key = "note:" + slug
        doc = new_document(self.conn, self.group_id, "note", "agent", key)
        rid = revision(self.conn, doc, title[:120], body,
                       {"origin_text": body}, publish=True)
        append_event("memory.write", group_id=self.group_id, table="knowledge_note",
                     note=key, chars=len(body))
        return f"note written: {key} revision={rid} chars={len(body)}"


# --- update_persona_drift --------------------------------------------------


_UPDATE_PERSONA_DRIFT_SPEC = ToolSpec(
    name="update_persona_drift",
    description=(
        "Replace this group's persona drift (the editable supplement to the "
        "static persona core). Read first with `read_persona_drift` and "
        "merge — overwriting wholesale loses prior calibration. Reserve "
        "this for genuine shifts in how you should behave in this group: "
        "e.g. group asked you to be terser, or you noticed a topic you "
        "should always volunteer to help with. Most agent runs do NOT "
        "warrant a drift update."
    ),
    parameters={
        "type": "object",
        "properties": {
            "drift_text": {
                "type": "string",
                "description": "Full new drift text. Replaces the existing entry. Keep concise — terse rules, not narrative.",
            },
        },
        "required": ["drift_text"],
    },
)


_DRIFT_TEXT_MAX = 4000


@dataclass
class UpdatePersonaDriftTool(Tool):
    spec = _UPDATE_PERSONA_DRIFT_SPEC
    conn: sqlite3.Connection
    group_id: str
    session: MemoryWriteSession

    def call(self, args: dict[str, Any]) -> str:
        text = args.get("drift_text")
        if not isinstance(text, str):
            raise ToolError("drift_text must be a string")
        text = text.strip()
        if len(text) > _DRIFT_TEXT_MAX:
            raise ToolError(
                f"drift_text too long ({len(text)} chars; max {_DRIFT_TEXT_MAX})"
            )
        previous = get_persona_drift(self.conn, self.group_id)
        if self.session.persona_hash is None:
            raise ToolError(
                "read_persona_drift must be called immediately before update_persona_drift"
            )
        if _text_hash(previous) != self.session.persona_hash:
            # Invalidate the snapshot so a blind retry without re-read
            # cannot pass the check (and would clobber the concurrent write).
            # Forces the model to call read_persona_drift again before retrying.
            self.session.persona_hash = None
            raise ToolError(
                "persona_drift changed since you read it. Call read_persona_drift again, "
                "merge your update with the current text, then retry."
            )
        upsert_persona_drift(self.conn, self.group_id, text)
        self.session.persona_hash = _text_hash(text)
        append_event(
            "memory.write",
            group_id=self.group_id,
            table="persona_drift",
            prev_len=len(previous),
            new_len=len(text),
        )
        return (
            f"persona drift updated. prev_len={len(previous)} new_len={len(text)}"
        )


# --- factory ---------------------------------------------------------------


def phase_b_system_prompt() -> str:
    """Reflection-phase system prompt (the static "what to do in Phase B"
    instructions). Persona module composes this with voice/identity from yaml.
    Source text lives in `prompts.PHASE_B_SYSTEM`."""
    return prompts.PHASE_B_SYSTEM


def register_phase_b_tools(tools: "GroupScopedTools") -> None:  # noqa: F821 - structural-only ref
    """Register Phase B tools: two writers + the two readers needed for
    replace-on-write."""
    session = MemoryWriteSession()
    tools.register(ReadPersonaDriftTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(ReadGroupMemoryForWriteTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(UpdatePersonaDriftTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(UpdateGroupMemoryTool(
        conn=tools.conn, group_id=tools.group_id, session=session,
    ))
    tools.register(WriteNoteTool(
        conn=tools.conn, group_id=tools.group_id,
        group_name=getattr(tools, "group_name", "") or "",
    ))


# --- trace inspection (used by dispatcher to know which memory rows to link) ---


def trace_touched_tables(phase_b_trace: list[dict[str, Any]] | None) -> tuple[bool, bool]:
    """Walk the Phase B trace and return (touched_persona_drift, touched_group_memory).
    Used by dispatcher.chat_via_agent post-run to UPDATE last_run_id pointers
    on the right rows."""
    if not phase_b_trace:
        return (False, False)
    persona = memory = False
    for step in phase_b_trace:
        if step.get("kind") != "tool_call":
            continue
        name = step.get("tool")
        if name == "update_persona_drift":
            persona = True
        elif name == "update_group_memory":
            memory = True
    return (persona, memory)
