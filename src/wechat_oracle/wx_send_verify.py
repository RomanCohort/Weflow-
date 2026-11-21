"""Confirm that a WeChat UI send reached the local conversation store.

The UI layer can prove that a paste and Enter key were issued, but not that
WeChat accepted the message. This module restores the post-send check used by
the former transport: poll Oracle's SQLite mirror first, then WeFlow's API.
"""

from __future__ import annotations

import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

from loguru import logger

from .config import settings
from .db import get_conn

DEFAULT_CONFIRM_TIMEOUT = 25.0
MIN_TEXT_SIMILARITY = 0.72


def normalize_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return "".join(normalized.split())


def text_matches(expected: str | None, actual: str | None) -> bool:
    left = normalize_text(expected)
    right = normalize_text(actual)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= MIN_TEXT_SIMILARITY


def latest_message_id(chat_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(msg_id), 0) FROM messages WHERE group_id=?",
            (chat_id,),
        ).fetchone()
    return int(row[0] if row else 0)


def _database_rows(chat_id: str, baseline: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT content_text, type, sender_wxid
              FROM messages
             WHERE group_id=? AND msg_id>?
             ORDER BY msg_id DESC
             LIMIT 100
            """,
            (chat_id, baseline),
        ).fetchall()
    return [dict(row) for row in rows]


def _weflow_rows(client, chat_id: str, since_ts: int) -> list[dict[str, Any]]:
    response = client.get(
        "/api/v1/messages",
        params={
            "talker": chat_id,
            "start": str(max(0, since_ts - 1)),
            "limit": 100,
            "offset": 0,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return list(response.json().get("messages") or [])


def _is_bot_sender(sender: str | None) -> bool:
    bot_wxid = (settings.bot_wxid or "").strip()
    return bool(bot_wxid and sender == bot_wxid)


def _authored_row(
    row: dict[str, Any],
    *,
    expected_text: str | None,
    allow_any_outgoing: bool,
    sender_key: str,
) -> bool:
    sender = str(row.get(sender_key) or "")
    if allow_any_outgoing:
        return _is_bot_sender(sender) if settings.bot_wxid else True
    if expected_text is None:
        return False
    candidates: Iterable[str] = (
        str(row.get("content_text") or ""),
        str(row.get("content") or ""),
        str(row.get("rawContent") or ""),
    )
    if settings.bot_wxid and sender and not _is_bot_sender(sender):
        return False
    return any(text_matches(expected_text, candidate) for candidate in candidates)


def confirm_send_recorded(
    chat_id: str,
    baseline: int,
    expected_text: str | None,
    *,
    since_ts: int,
    allow_any_outgoing: bool = False,
    timeout: float = DEFAULT_CONFIRM_TIMEOUT,
) -> bool:
    """Return whether a new matching/outgoing record appears after ``baseline``."""

    deadline = time.monotonic() + max(0.1, timeout)
    weflow = None
    next_weflow_check = 0.0
    try:
        try:
            if settings.weflow_token:
                from .ingest.live import _build_client

                weflow = _build_client()
        except Exception as exc:
            logger.debug("send confirmation API unavailable: {}", exc)

        while time.monotonic() < deadline:
            try:
                for row in _database_rows(chat_id, baseline):
                    if _authored_row(
                        row,
                        expected_text=expected_text,
                        allow_any_outgoing=allow_any_outgoing,
                        sender_key="sender_wxid",
                    ):
                        return True
            except Exception as exc:
                logger.debug("send confirmation DB read failed: {}", exc)

            now = time.monotonic()
            if weflow is not None and now >= next_weflow_check:
                next_weflow_check = now + 1.0
                try:
                    for row in _weflow_rows(weflow, chat_id, since_ts):
                        if _authored_row(
                            row,
                            expected_text=expected_text,
                            allow_any_outgoing=allow_any_outgoing,
                            sender_key="senderUsername",
                        ):
                            return True
                except Exception as exc:
                    logger.debug("send confirmation API read failed: {}", exc)

            time.sleep(0.5)
    finally:
        if weflow is not None:
            weflow.close()
    return False
