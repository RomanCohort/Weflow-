"""Reply backends for the dispatcher.

UI automation is delegated to the project's official ``wx4py`` backend:
``chat_window.send_to()`` searches for and opens the target chat, then pastes
and submits the message through WeChat's UIA controls.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from loguru import logger

from .config import settings
from .message_render import is_degenerate_outbound, repair_outbound
from .wx_send_verify import confirm_send_recorded, latest_message_id

_AT_SEP = "\u2005"


def _configure_wx4py_loggers() -> None:
    level = getattr(logging, settings.wx4py_log_level.upper(), logging.WARNING)
    for name in (
        "wx4py",
        "wx4py.client",
        "wx4py.core",
        "wx4py.core.window",
        "wx4py.features",
        "wx4py.features.chat",
    ):
        logging.getLogger(name).setLevel(level)


def _with_wx4py_info_suppressed(fn):
    if settings.wx4py_log_level.upper() in ("DEBUG", "INFO"):
        return fn()
    old_disable = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        return fn()
    finally:
        logging.disable(old_disable)


def _strip_leading_requester_mention(text: str, requester: str | None) -> str:
    if not requester:
        return text
    body = text.lstrip()
    prefix = f"@{requester}"
    if not body.startswith(prefix):
        return text
    return body[len(prefix):].lstrip(" \t\r\n\u2005")


class Replier(Protocol):
    def send(
        self,
        group_name: str | None,
        requester: str | None,
        text: str,
        *,
        target_type: str = "group",
        target: str | None = None,
        target_id: str | None = None,
    ) -> None: ...

    def send_file(
        self,
        group_name: str | None,
        path,
        message: str | None = None,
        *,
        target_id: str | None = None,
    ) -> None: ...

    def disconnect(self) -> None: ...


class ReplySendError(RuntimeError):
    """Raised when wx4py proves that delivery did not succeed."""


class StdoutReplier:
    def send(
        self,
        group_name: str | None,
        requester: str | None,
        text: str,
        *,
        target_type: str = "group",
        target: str | None = None,
        target_id: str | None = None,
    ) -> None:
        if is_degenerate_outbound(text):
            logger.warning("blocked degenerate stdout text: chars={}", len(text))
            text = repair_outbound(text)
        logger.debug("stdout-replier: would send to {}: {!r}", group_name, text[:80])

    def send_file(
        self,
        group_name: str | None,
        path,
        message: str | None = None,
        *,
        target_id: str | None = None,
    ) -> None:
        logger.debug(
            "stdout-replier: would send file to {}: name={!r}",
            group_name,
            Path(path).name,
        )

    def disconnect(self) -> None:
        pass


class Wx4pyReplier:
    """Official ``wx4py`` UI automation backend."""

    def __init__(self, wx) -> None:
        self._wx = wx

    @staticmethod
    def _destination(
        group_name: str | None,
        target: str | None,
        target_id: str | None,
    ) -> str:
        if not target_id:
            raise ReplySendError("missing stable conversation id")
        destination = (target or group_name or "").strip()
        if not destination:
            raise ReplySendError("missing conversation name")
        return destination

    def send(
        self,
        group_name: str | None,
        requester: str | None,
        text: str,
        *,
        target_type: str = "group",
        target: str | None = None,
        target_id: str | None = None,
    ) -> None:
        destination = self._destination(group_name, target, target_id)
        if is_degenerate_outbound(text):
            logger.warning("blocked degenerate outbound text: chars={}", len(text))
            text = repair_outbound(text)
        text = _strip_leading_requester_mention(text, requester)
        baseline = latest_message_id(target_id)
        since_ts = int(time.time())
        hwnd, previous = self._prepare_window()
        sent_ok = False
        confirmed_body = text
        try:
            if (
                target_type == "group"
                and requester
                and self._send_group_mention(destination, requester, text)
            ):
                sent_ok = True
                confirmed_body = f"@{requester}{_AT_SEP}{text}"
            else:
                confirmed_body = (
                    f"@{requester}{_AT_SEP}{text}"
                    if requester and target_type == "group"
                    else text
                )
                sent_ok = self._wx.chat_window.send_to(
                    destination,
                    confirmed_body,
                    target_type=target_type,
                )
        except Exception as exc:
            raise ReplySendError(
                f"wx4py send_to failed for {group_name!r}: {type(exc).__name__}"
            ) from exc
        finally:
            self._restore_window(hwnd, previous)
        if not sent_ok:
            raise ReplySendError(
                f"wx4py send_to returned false for {group_name!r}"
            )
        self._confirm_send(target_id, baseline, confirmed_body, since_ts)

    def send_file(
        self,
        group_name: str | None,
        path,
        message: str | None = None,
        *,
        target_id: str | None = None,
    ) -> None:
        destination = self._destination(group_name, group_name, target_id)
        if message and is_degenerate_outbound(message):
            logger.warning(
                "blocked degenerate attachment caption: chars={}", len(message)
            )
            message = repair_outbound(message)
        file_path = Path(path)
        if not file_path.is_file() or file_path.is_symlink():
            raise ReplySendError("attachment file is unavailable")
        baseline = latest_message_id(target_id)
        since_ts = int(time.time())
        hwnd, previous = self._prepare_window()
        try:
            sent = self._wx.chat_window.send_file_to(
                destination,
                str(file_path),
                target_type="group",
                message=message,
            )
        except Exception as exc:
            logger.warning("wx4py send_file_to raised: {}", exc)
            raise ReplySendError(
                f"wx4py send_file_to failed for {group_name!r}: "
                f"{type(exc).__name__}"
            ) from exc
        finally:
            self._restore_window(hwnd, previous)
        if not sent:
            raise ReplySendError(
                f"wx4py send_file_to returned false for {group_name!r}"
            )
        self._confirm_send(
            target_id,
            baseline,
            message or file_path.name,
            since_ts,
            allow_any_outgoing=True,
        )

    def _prepare_window(self) -> tuple[int, int]:
        hwnd = int(getattr(self._wx.window, "hwnd", 0) or 0)
        if not hwnd:
            raise ReplySendError("WeChat window handle is unavailable")
        from .wx_preview_capture import bring_wechat_to_front

        return hwnd, bring_wechat_to_front(hwnd)

    @staticmethod
    def _restore_window(hwnd: int, previous: int) -> None:
        if not hwnd:
            return
        from .wx_preview_capture import restore_wechat_window

        restore_wechat_window(hwnd, previous)

    @staticmethod
    def _confirm_send(
        target_id: str,
        baseline: int,
        expected_text: str,
        since_ts: int,
        *,
        allow_any_outgoing: bool = False,
    ) -> None:
        if confirm_send_recorded(
            target_id,
            baseline,
            expected_text,
            since_ts=since_ts,
            allow_any_outgoing=allow_any_outgoing,
        ):
            return
        raise ReplySendError(
            "WeChat accepted the UI action but the message was not confirmed "
            "in the local conversation store"
        )

    def capture_image_preview(
        self,
        group_id: str,
        group_name: str,
        body: str,
        sender: str,
        deadline: float,
    ) -> bytes | None:
        from .wx_preview_capture import capture_quote_preview

        return capture_quote_preview(
            self._wx,
            group_id,
            group_name,
            body,
            sender,
            deadline,
        )

    def _send_group_mention(
        self,
        group_name: str,
        requester: str,
        text: str,
    ) -> bool:
        try:
            chat = self._wx.chat_window
            if not chat.open_chat(group_name, target_type="group"):
                return False
            edit = chat.prepare_input_for_paste(chat._get_chat_input())
            if not edit:
                return False

            before = self._mention_candidate_signatures(chat.root, requester)
            edit.SendKeys("@")
            time.sleep(0.2)
            if not edit.GetValuePattern().Value.endswith("@"):
                return False
            if not chat.paste_text_into_focused_input(
                requester,
                log_error="写入 @ 对象到剪贴板失败",
            ):
                return False
            time.sleep(0.5)
            after = self._mention_candidate_signatures(chat.root, requester)
            if not (after - before):
                logger.warning(
                    "wx4py @ candidate not visible for requester={!r}",
                    requester,
                )
                try:
                    edit.SendKeys("{Ctrl}a")
                    edit.SendKeys("{Delete}")
                except Exception:
                    pass
                return False
            edit.SendKeys("{Enter}")
            time.sleep(0.2)
            suffix = text.strip()
            if suffix and not chat.paste_text_into_focused_input(
                _AT_SEP + suffix,
                log_error="写入 @ 回复正文到剪贴板失败",
            ):
                return False
            edit.SendKeys("{Enter}")
            time.sleep(0.3)
            return True
        except Exception as exc:
            logger.warning(
                "wx4py real @ mention failed (group={!r}, requester={!r}): {}",
                group_name,
                requester,
                exc,
            )
            return False

    @staticmethod
    def _mention_candidate_signatures(
        root,
        requester: str,
    ) -> set[tuple[str, str, int, int]]:
        target = requester.strip()
        if not target:
            return set()
        found: set[tuple[str, str, int, int]] = set()

        def walk(ctrl, depth: int) -> None:
            if depth > 8 or ctrl is None:
                return
            try:
                name = (ctrl.Name or "").strip()
                control_type = ctrl.ControlTypeName or ""
                if target in name and control_type != "EditControl":
                    rect = ctrl.BoundingRectangle
                    found.add((
                        name,
                        control_type,
                        int(getattr(rect, "left", 0) or 0),
                        int(getattr(rect, "top", 0) or 0),
                    ))
            except Exception:
                pass
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for child in children:
                walk(child, depth + 1)

        walk(root, 0)
        return found

    def disconnect(self) -> None:
        try:
            self._wx.disconnect()
        except Exception as exc:
            logger.warning("wx4py disconnect failed: {}", exc)

    @classmethod
    def try_connect(cls) -> Replier | None:
        _configure_wx4py_loggers()
        try:
            from wx4py import WeChatClient
        except ImportError:
            logger.warning("wx4py is not installed")
            return None
        try:
            wx = _with_wx4py_info_suppressed(WeChatClient)
            _configure_wx4py_loggers()
            _with_wx4py_info_suppressed(wx.connect)
        except Exception as exc:
            logger.warning(
                "wx4py connect failed ({}); replies disabled this run",
                exc,
            )
            return None
        return cls(wx)


def build_replier() -> Replier:
    if not settings.reply:
        logger.info("WO_REPLY=False; using stdout replier")
        return StdoutReplier()

    backend = (settings.reply_backend or "wx4py").lower()
    if backend == "stdout":
        return StdoutReplier()
    if backend == "wx4py":
        return Wx4pyReplier.try_connect() or StdoutReplier()

    logger.warning(
        "unknown WO_REPLY_BACKEND={!r}; valid: wx4py / stdout. Using stdout.",
        backend,
    )
    return StdoutReplier()
