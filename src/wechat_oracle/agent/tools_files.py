"""Controlled per-group file tools: safe paths, atomic creation, read-back."""
from __future__ import annotations

import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm

_TEXT_SUFFIXES = {".md", ".txt", ".csv", ".json"}
_ALL_SUFFIXES = _TEXT_SUFFIXES | {".png", ".pdb"}
_MAX_TEXT_CHARS = 200_000
_MAX_READ_CHARS = 20_000


def _group_root(group_id: str) -> Path:
    """Safe per-group component: reject separators, drive roots, and traversal."""
    if not group_id or group_id in {".", ".."}:
        raise ToolError("invalid bound group id")
    if Path(group_id).name != group_id or "/" in group_id or "\\" in group_id:
        raise ToolError("invalid bound group id")
    if Path(group_id).drive or ":" in group_id or group_id.startswith(("/", "\\")):
        raise ToolError("invalid bound group id")
    return Path(group_id)


def group_output_root(group_id: str) -> Path:
    """Resolve a new-output directory under the configured data root."""
    root = settings.agent_output_dir
    if not root.is_absolute():
        root = settings.data_dir.parent / root if root.parts[0] == "data" else Path.cwd() / root
    group = root.resolve() / _group_root(group_id)
    if group.is_symlink() or group.is_junction():
        raise ToolError("group output directory must not be a link")
    return group


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Create a new file atomically; never replace an existing file."""
    fd, tmp_name = tempfile.mkstemp(prefix=".wechat-oracle-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # FileExistsError means another writer won; no overwrite.
    finally:
        tmp.unlink(missing_ok=True)


def _safe_output_path(group_id: str, filename: str, *, text_only: bool = False) -> Path:
    allowed = _TEXT_SUFFIXES if text_only else _ALL_SUFFIXES
    name = str(filename or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ToolError("filename must be a plain file name")
    if "/" in name or "\\" in name or ":" in name or name.startswith("."):
        raise ToolError("filename contains an unsupported path component")
    if Path(name).suffix.lower() not in allowed:
        raise ToolError("unsupported file type; allowed: " + ", ".join(sorted(allowed)))
    root = group_output_root(group_id)
    if root.is_symlink() or root.is_junction():
        raise ToolError("group output directory must not be a link")
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    if path.exists() or path.is_symlink():
        raise ToolError("file already exists; choose a new filename")
    return path


def _existing_output_path(group_id: str, filename: str) -> Path:
    name = str(filename or "").strip()
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ToolError("filename must be a plain file name")
    if "/" in name or "\\" in name or ":" in name:
        raise ToolError("filename contains an unsupported path component")
    root = group_output_root(group_id)
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise ToolError("file not found in this group's output directory")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as e:
        raise ToolError("file is outside this group's output directory") from e
    if path.suffix.lower() not in _ALL_SUFFIXES:
        raise ToolError("unsupported file type")
    return path


def _new_output_name(prefix: str, suffix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}{suffix}"


_WRITE_TEXT_SPEC = ToolSpec(
    name="write_output_file",
    description=(
        "Create a new file in this group's private output directory. Use this "
        "for deliverables the group asked for: reports, notes, CSV/JSON data. "
        "Supported suffixes: .md .txt .csv .json. Existing files are never "
        "overwritten. Returns the generated filename; call send_output_file "
        "only when the user asked for the file to be sent back."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Plain filename with .md/.txt/.csv/.json suffix; no folders."},
            "content": {"type": "string", "description": "Full UTF-8 file content."},
        },
        "required": ["filename", "content"],
    },
)


@dataclass
class WriteOutputFileTool(Tool):
    spec = _WRITE_TEXT_SPEC
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise ToolError("filename must be a string")
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        if len(content) > _MAX_TEXT_CHARS:
            raise ToolError(f"content too long ({len(content)} chars; max {_MAX_TEXT_CHARS})")
        path = _safe_output_path(self.group_id, filename, text_only=True)
        data = content.encode("utf-8")
        if len(data) > settings.agent_output_max_bytes:
            raise ToolError(
                f"file too large ({len(data)} bytes; max {settings.agent_output_max_bytes})"
            )
        try:
            atomic_write_bytes(path, data)
        except FileExistsError as e:
            raise ToolError("file already exists; choose a new filename") from e
        return f"created {path.name} ({len(data)} bytes); not sent yet"


_LIST_SPEC = ToolSpec(
    name="list_output_files",
    description=(
        "List files previously created in this group's private output "
        "directory, with sizes. Use it to find a filename for "
        "read_output_file or send_output_file."
    ),
    parameters={"type": "object", "properties": {}},
)


@dataclass
class ListOutputFilesTool(Tool):
    spec = _LIST_SPEC
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        root = group_output_root(self.group_id)
        if not root.is_dir():
            return "(no output files yet)"
        files = []
        for path in sorted(root.iterdir(), key=lambda p: p.name):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _ALL_SUFFIXES:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(f"{path.name}\t{size} bytes")
        if not files:
            return "(no output files yet)"
        return truncate_for_llm("\n".join(files), limit=4000)


_READ_SPEC = ToolSpec(
    name="read_output_file",
    description=(
        "Read a text file from this group's private output directory. "
        "Supported suffixes: .md .txt .csv .json. Use list_output_files "
        "first if you do not know the exact filename."
    ),
    parameters={
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "Exact filename from list_output_files."}},
        "required": ["filename"],
    },
)


@dataclass
class ReadOutputFileTool(Tool):
    spec = _READ_SPEC
    group_id: str

    def call(self, args: dict[str, Any]) -> str:
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise ToolError("filename must be a string")
        path = _existing_output_path(self.group_id, filename)
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            raise ToolError("read_output_file only supports text files")
        if path.stat().st_size > settings.agent_output_max_bytes:
            raise ToolError("file exceeds the output byte cap")
        text = path.read_text(encoding="utf-8")
        return truncate_for_llm(text, limit=_MAX_READ_CHARS)


_SEND_SPEC = ToolSpec(
    name="send_output_file",
    description=(
        "Send one file from this group's private output directory back to "
        "this same group through the verified WeChat attachment path. Use "
        "only when the user asked for a file/report/attachment; this call "
        "actually sends it and can fail if WeChat does not confirm delivery. "
        "Do not call twice for the same file in one turn without a new user "
        "request, because delivery confirmation may be delayed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Exact filename from list_output_files."},
            "message": {"type": "string", "description": "Optional short caption shown with the attachment."},
        },
        "required": ["filename"],
    },
)


@dataclass
class SendOutputFileTool(Tool):
    spec = _SEND_SPEC
    group_id: str
    group_name: str | None
    attachment_sender: Any
    sent_files: set[str]

    def call(self, args: dict[str, Any]) -> str:
        filename = args.get("filename")
        if not isinstance(filename, str):
            raise ToolError("filename must be a string")
        message = args.get("message")
        if message is not None and not isinstance(message, str):
            raise ToolError("message must be a string")
        path = _existing_output_path(self.group_id, filename)
        cap = (
            settings.krea2_max_image_bytes
            if path.suffix.lower() == ".png"
            else settings.agent_output_max_bytes
        )
        if path.stat().st_size > cap:
            raise ToolError("file exceeds the attachment byte cap")
        if path.name in self.sent_files:
            raise ToolError("this file was already submitted for sending in this turn")
        try:
            self.attachment_sender(self.group_name, path, message or None)
        except Exception as e:
            raise ToolError(
                f"file delivery failed for {path.name}: {type(e).__name__}: {e}"
            ) from e
        self.sent_files.add(path.name)
        return (
            f"sent {path.name} to this group and delivery was confirmed; "
            "do not resend it in this turn"
        )


def register_output_file_tools(
    tools: Any,
    *,
    attachment_sender: Any = None,
) -> None:
    """Register controlled output-file tools.

    Read/write/list are available in direct native turns. The send tool is
    registered only when the dispatcher supplied a group-bound sender.
    """
    tools.register(WriteOutputFileTool(group_id=tools.group_id))
    tools.register(ListOutputFilesTool(group_id=tools.group_id))
    tools.register(ReadOutputFileTool(group_id=tools.group_id))
    if attachment_sender is not None:
        tools.register(SendOutputFileTool(
            group_id=tools.group_id,
            group_name=tools.group_name,
            attachment_sender=attachment_sender,
            sent_files=set(),
        ))


__all__ = [
    "atomic_write_bytes",
    "group_output_root",
    "register_output_file_tools",
]
