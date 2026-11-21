"""Controlled inbound file discovery and reading.

The WeChat file cache stores received files unencrypted under
``<xwechat_files>/<account>/msg/file/YYYY-MM``.  WeFlow does not expose a
general attachment download API, so we identify inbound files from the
current group's already-ingested messages, then resolve them by exact file
name inside that cache root.  The LLM never receives a raw filesystem path.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..config import settings
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".py", ".js", ".ts"}
MAX_INBOUND_BYTES = 5_000_000
MAX_READ_CHARS = 20_000
MAX_LIST_ITEMS = 100

_FILE_MSG_RE = re.compile(r"^\s*\[(?:文件|file)\]\s*(.+?)\s*$", re.I)


def normalize_filename(value: str) -> str:
    """Normalize Unicode and separators for exact-ish name matching."""
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise ToolError("invalid file name")
    if "\x00" in name:
        raise ToolError("invalid file name")
    return name


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    configured = getattr(settings, "wechat_db_path", None) or getattr(settings, "wechat_files_dir", None)
    if configured:
        roots.append(Path(configured))
    roots.extend(
        [
            Path("C:/Users/dev/Documents/xwechat_files"),
            Path("C:/Users/LENOVO/Documents/xwechat_files"),
        ]
    )
    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        key = str(root.resolve()).casefold()
        if key not in seen and root.is_dir():
            seen.add(key)
            result.append(root)
    return result


def _message_file_names(conn: sqlite3.Connection, group_id: str, limit: int) -> list[str]:
    """Extract recent file names from this group's ingested messages only."""
    rows = conn.execute(
        """
        SELECT content_text, media_path
        FROM messages
        WHERE group_id=? AND (type='file' OR content_text LIKE '[文件]%' OR content_text LIKE '[file]%')
        ORDER BY msg_id DESC
        LIMIT ?
        """,
        (group_id, max(limit * 4, 200)),
    ).fetchall()
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        candidates: list[str] = []
        content = row["content_text"] or ""
        match = _FILE_MSG_RE.match(content)
        if match:
            candidates.append(match.group(1))
        media_path = row["media_path"]
        if media_path:
            candidates.append(str(media_path))
        for candidate in candidates:
            try:
                name = normalize_filename(candidate)
            except ToolError:
                continue
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(name)
            if len(names) >= limit:
                return names
    return names


def _iter_cache_files(root: Path) -> Iterable[Path]:
    """Yield plain files under account msg/file trees, skipping links."""
    for account in root.iterdir():
        if not account.is_dir() or account.is_symlink():
            continue
        file_root = account / "msg" / "file"
        if not file_root.is_dir() or file_root.is_symlink():
            continue
        for path in file_root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def find_inbound_files(conn: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    """Find cache files whose names appear in this group's message history."""
    wanted = _message_file_names(conn, group_id, MAX_LIST_ITEMS)
    if not wanted:
        return []
    by_name = {name.casefold(): name for name in wanted}
    matches: dict[str, dict[str, Any]] = {}
    for root in _candidate_roots():
        for path in _iter_cache_files(root):
            key = path.name.casefold()
            if key not in by_name or key in matches:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_INBOUND_BYTES:
                matches[key] = {
                    "name": path.name,
                    "size": stat.st_size,
                    "readable": False,
                    "reason": f"file exceeds {MAX_INBOUND_BYTES} byte read cap",
                }
                continue
            matches[key] = {
                "name": path.name,
                "size": stat.st_size,
                "readable": path.suffix.lower() in TEXT_SUFFIXES,
                "reason": "" if path.suffix.lower() in TEXT_SUFFIXES else "binary file; text read not supported",
            }
    return [matches[key] for key in (n.casefold() for n in wanted) if key in matches]


def _resolve_inbound_path(conn: sqlite3.Connection, group_id: str, filename: str) -> Path:
    name = normalize_filename(filename)
    wanted = {n.casefold() for n in _message_file_names(conn, group_id, MAX_LIST_ITEMS)}
    if name.casefold() not in wanted:
        raise ToolError("file is not in this group's inbound file list")
    found: Path | None = None
    for root in _candidate_roots():
        for path in _iter_cache_files(root):
            if path.name.casefold() == name.casefold():
                if found is not None:
                    raise ToolError("ambiguous file name; download it manually")
                found = path
    if found is None:
        raise ToolError("inbound file bytes not found in the WeChat file cache")
    try:
        size = found.stat().st_size
    except OSError as e:
        raise ToolError("cannot stat inbound file") from e
    if size > MAX_INBOUND_BYTES:
        raise ToolError(f"file exceeds {MAX_INBOUND_BYTES} byte read cap")
    return found


_LIST_SPEC = ToolSpec(
    name="list_inbound_files",
    description=(
        "List files received in this group that are available in the local "
        "WeChat file cache. Call this before read_inbound_file when the user "
        "says they sent a file or asks you to inspect an attachment."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ListInboundFilesTool(Tool):
    spec = _LIST_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        files = find_inbound_files(self.conn, self.group_id)
        if not files:
            return "(no inbound files found for this group)"
        lines = []
        for item in files:
            suffix = "" if item["readable"] else f" - {item['reason']}"
            lines.append(f"{item['name']}\t{item['size']} bytes{suffix}")
        return truncate_for_llm("\n".join(lines), limit=4000)


_READ_SPEC = ToolSpec(
    name="read_inbound_file",
    description=(
        "Read a text file received in this group from the local WeChat file "
        "cache. Use list_inbound_files first to get the exact file name. "
        "Only text-like files are supported."
    ),
    parameters={
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "Exact file name from list_inbound_files."}},
        "required": ["filename"],
    },
)


@dataclass
class ReadInboundFileTool(Tool):
    spec = _READ_SPEC
    conn: sqlite3.Connection
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise ToolError("filename must be a string")
        path = _resolve_inbound_path(self.conn, self.group_id, filename)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise ToolError("only text-like inbound files can be read with this tool")
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
        header = f"filename: {path.name}\nsize: {len(data)} bytes\n---\n"
        return header + truncate_for_llm(text, limit=MAX_READ_CHARS)


def register_inbound_file_tools(tools: Any) -> None:
    """Register inbound file tools bound to the current group."""
    tools.register(ListInboundFilesTool(conn=tools.conn, group_id=tools.group_id))
    tools.register(ReadInboundFileTool(conn=tools.conn, group_id=tools.group_id))


__all__ = [
    "find_inbound_files",
    "register_inbound_file_tools",
]
