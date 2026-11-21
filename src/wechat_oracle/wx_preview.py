"""Database and filesystem helpers for cached quoted-image previews."""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

PREVIEW_PREFIX = "wx-preview-"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 12 * 1024 * 1024


def _text(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFC", value or "").split())


def _valid_group_id(group_id: str | None) -> bool:
    return bool(
        isinstance(group_id, str)
        and group_id.endswith("@chatroom")
        and re.fullmatch(r"[A-Za-z0-9_.@-]+", group_id)
    )


def quote_for_preview(conn, msg_id: int, group_id: str):
    """Return a unique live quote and its image source, if still unresolved."""
    rows = conn.execute(
        """
        SELECT q.content_text, p.sender_display, p.msg_id, p.wx_msg_id,
               p.media_path
          FROM messages q JOIN messages p
            ON p.group_id = q.group_id AND p.wx_msg_id = q.reply_to_wx_msg_id
         WHERE q.msg_id = ? AND q.group_id = ? AND q.type = 'quote'
           AND q.source = 'live' AND p.type = 'image'
        """,
        (msg_id, group_id),
    ).fetchmany(2)
    if len(rows) != 1 or not _valid_group_id(group_id):
        return None
    body, sender, source_id, source_wx_id, media_path = rows[0]
    body = _text(body)
    sender = _text(sender)
    if media_path or not sender or sum(char.isalnum() for char in body) < 6:
        return None
    return group_id, body, sender, source_id, source_wx_id


def cached_preview_path(data_dir: Path, group_id: str, msg_id: int) -> Path | None:
    if not _valid_group_id(group_id):
        return None
    file = (
        data_dir / "media" / group_id / "images" / f"{PREVIEW_PREFIX}{msg_id}.png"
    )
    if not file.is_file() or file.is_symlink():
        return None
    return file if file.resolve().is_relative_to(data_dir.resolve()) else None


def attach_preview(
    conn: sqlite3.Connection,
    data_dir: Path,
    group_id: str,
    source_id: int,
    source_wx_id: str,
    data: bytes,
) -> bool:
    if (
        not _valid_group_id(group_id)
        or not data.startswith(PNG_HEADER)
        or len(data) > MAX_BYTES
    ):
        return False
    if not conn.execute(
        """SELECT 1 FROM messages WHERE msg_id = ? AND group_id = ?
              AND wx_msg_id = ? AND type = 'image' AND media_path IS NULL""",
        (source_id, group_id, source_wx_id),
    ).fetchone():
        return False
    root = data_dir.resolve()
    destination = (
        root / "media" / group_id / "images" / f"{PREVIEW_PREFIX}{source_id}.png"
    )
    if not destination.resolve().is_relative_to(root):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".preview-", dir=destination.parent)
    temporary = Path(path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return cached_preview_path(data_dir, group_id, source_id) is not None
        return True
    finally:
        temporary.unlink(missing_ok=True)
