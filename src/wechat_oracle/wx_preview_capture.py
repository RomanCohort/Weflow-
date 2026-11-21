"""Capture an uncached quoted image from the visible WeChat conversation.

This is the narrow, read-only part of the former OCR transport. It uses the
official wx4py client to open the chat, RapidOCR to locate the quoted message,
and screen capture to return the image card or enlarged viewer.
"""

from __future__ import annotations

import io
import re
import time
import unicodedata
from threading import Lock
from typing import Any

from loguru import logger

MAX_PREVIEW_BYTES = 12 * 1024 * 1024
_dpi_ready = False
_ocr_engine: Any = None
_ocr_lock = Lock()


def _set_dpi_awareness() -> None:
    global _dpi_ready
    if _dpi_ready:
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_ready = True


def _get_ocr_engine() -> Any:
    global _ocr_engine
    if _ocr_engine is None:
        with _ocr_lock:
            if _ocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _ocr_engine = RapidOCR()
    return _ocr_engine


def _normalize_ocr(value: str) -> str:
    text = unicodedata.normalize("NFC", value or "").casefold()
    return "".join(char for char in text if char.isalnum())


def body_fragment(body: str) -> str:
    parts = [
        part for part in re.split(r"[\s\u2005]+", body or "") if part.strip()
    ]
    candidate = parts[-1] if len(parts) > 1 else (body or "")
    normalized = _normalize_ocr(candidate)
    return normalized if len(normalized) >= 2 else _normalize_ocr(body)[-4:]


def quote_row(
    rows: list[tuple[str, int, int, int, int]],
    body: str,
    sender: str,
    height: int,
) -> tuple[str, int, int, int, int] | None:
    fragment = body_fragment(body)
    sender_key = _normalize_ocr(sender)
    candidates: list[tuple[int, int, tuple[str, int, int, int, int]]] = []
    for row in rows:
        text, _x, y, _width, _height = row
        normalized = _normalize_ocr(text)
        if fragment and fragment in normalized:
            score = 2
        elif sender_key and sender_key in normalized and y > height * 0.45:
            score = 1
        else:
            continue
        candidates.append((score, y, row))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def image_region(image, preferred_y: int | None = None):
    """Best-effort rectangular image detection inside a quote-card crop."""
    try:
        import cv2
        import numpy as np

        array = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 45, 135)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
        contours, _ = cv2.findContours(
            edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        height, width = gray.shape[:2]
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            ratio = w / max(1, h)
            if (
                area < 700
                or w < 28
                or h < 28
                or w > width * 0.82
                or ratio < 0.35
                or ratio > 2.6
                or (preferred_y is not None and y < preferred_y - 20)
            ):
                continue
            candidates.append((area, x, y, w, h))
        if not candidates:
            return None
        _, x, y, w, h = max(candidates)
        return x, y, w, h
    except Exception as exc:
        logger.debug("quote thumbnail detection failed: {}", exc)
        return None


def _ocr_rows(image) -> list[tuple[str, int, int, int, int]]:
    from PIL import Image
    import numpy as np

    scale = 2
    enlarged = image.resize(
        (image.width * scale, image.height * scale),
        Image.Resampling.LANCZOS,
    )
    result, _elapsed = _get_ocr_engine()(np.array(enlarged))
    rows = []
    for item in result or []:
        if not item or len(item) < 2:
            continue
        box, raw_text = item[0], str(item[1] or "").strip()
        if not raw_text or len(box) < 4:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        rows.append(
            (
                raw_text,
                int(min(xs) / scale),
                int(min(ys) / scale),
                int((max(xs) - min(xs)) / scale),
                int((max(ys) - min(ys)) / scale),
            )
        )
    return rows


def _png_bytes(image) -> bytes | None:
    from PIL import Image

    candidate = image.convert("RGB")
    max_width = 1800
    if candidate.width > max_width:
        height = round(candidate.height * max_width / candidate.width)
        candidate = candidate.resize((max_width, height), Image.Resampling.LANCZOS)

    while True:
        buffer = io.BytesIO()
        candidate.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        if len(data) <= MAX_PREVIEW_BYTES:
            return data
        if candidate.width <= 640:
            return None
        candidate = candidate.resize(
            (round(candidate.width * 0.8), round(candidate.height * 0.8)),
            Image.Resampling.LANCZOS,
        )


def _window_bounds(root) -> tuple[int, int, int, int] | None:
    try:
        rect = root.BoundingRectangle
        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)
    except Exception:
        return None
    if right - left < 500 or bottom - top < 400:
        return None
    return left, top, right, bottom


def _walk_controls(root, max_depth: int = 20):
    stack = [(root, 0)]
    while stack:
        control, depth = stack.pop()
        if control is None or depth > max_depth:
            continue
        yield control, depth
        try:
            children = control.GetChildren()
        except Exception:
            continue
        for child in reversed(children):
            stack.append((child, depth + 1))


def _control_rect(control) -> tuple[int, int, int, int] | None:
    try:
        rect = control.BoundingRectangle
        values = (
            int(rect.left),
            int(rect.top),
            int(rect.right),
            int(rect.bottom),
        )
    except Exception:
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _find_refer_image_rect(
    root,
    body: str,
    sender: str,
) -> tuple[int, int, int, int] | None:
    fragment = body_fragment(body)
    sender_key = _normalize_ocr(sender)
    text_items = []
    image_items = []
    for control, _depth in _walk_controls(root):
        try:
            class_name = str(control.ClassName or "")
            name = str(control.Name or "")
            normalized = _normalize_ocr(name)
            rect = _control_rect(control)
        except Exception:
            continue
        if rect is None:
            continue
        if "ChatTextItemView" in class_name and (
            (fragment and fragment in normalized)
            or (sender_key and sender_key in normalized and "引用" in name)
        ):
            text_items.append((rect, name))
        elif "ChatBubbleReferItemView" in class_name and (
            "图片" in name or not name
        ):
            image_items.append((rect, name))

    for text_rect, _name in sorted(text_items, key=lambda item: item[0][3], reverse=True):
        candidates = [
            item
            for item in image_items
            if item[0][3] <= text_rect[1] + 12
            and abs(item[0][0] - text_rect[0]) <= 24
        ]
        if candidates:
            return max(candidates, key=lambda item: item[0][3])[0]
    if image_items:
        return max(image_items, key=lambda item: item[0][3])[0]
    return None


def _grab_bbox(bbox: tuple[int, int, int, int]):
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=bbox, all_screens=True)


def bring_wechat_to_front(hwnd: int) -> int:
    import win32gui
    import win32con

    previous = int(win32gui.GetForegroundWindow() or 0)
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_SHOWWINDOW,
    )
    win32gui.BringWindowToTop(hwnd)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.55)
    return previous


def restore_wechat_window(hwnd: int, previous: int) -> None:
    try:
        import win32gui
        import win32con

        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
        if previous and previous != hwnd and win32gui.IsWindow(previous):
            win32gui.SetForegroundWindow(previous)
    except Exception:
        pass


def _visual_change(before, after) -> float:
    from PIL import ImageChops, ImageStat

    if before.size != after.size:
        return 1.0
    diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
    mean = ImageStat.Stat(diff).mean
    return sum(mean) / (255.0 * max(1, len(mean)))


def _click_relative(client_left: int, client_top: int, x: int, y: int) -> None:
    import win32api
    import win32con

    win32api.SetCursorPos((client_left + x, client_top + y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _press_escape() -> None:
    try:
        import win32api
        import win32con

        win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(
            win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0
        )
    except Exception:
        pass


def capture_quote_preview(
    wx,
    group_id: str,
    group_name: str,
    body: str,
    sender: str,
    deadline: float,
) -> bytes | None:
    if time.monotonic() >= deadline or not group_name:
        return None

    hwnd = 0
    previous = 0
    try:
        _set_dpi_awareness()
        import win32gui

        chat = wx.chat_window
        target_type = "group" if group_id.endswith("@chatroom") else "contact"
        if not chat.open_chat(group_name, target_type=target_type):
            return None
        wx.window.activate()
        hwnd = wx.window.hwnd
        if not hwnd or not win32gui.IsWindowVisible(hwnd):
            return None
        previous = bring_wechat_to_front(hwnd)
        bounds = _window_bounds(chat.root)
        if bounds is None:
            return None

        refer_rect = _find_refer_image_rect(chat.root, body, sender)
        if refer_rect is not None:
            card = _grab_bbox(refer_rect)
            before_full = _grab_bbox(bounds)
            region = image_region(card)
            if region is None:
                return _png_bytes(card)
            x, y, width, height = region
            _click_relative(
                0,
                0,
                refer_rect[0] + x + width // 2,
                refer_rect[1] + y + height // 2,
            )
            time.sleep(0.65)
            after = _grab_bbox(bounds)
            if (
                after is not None
                and before_full is not None
                and _visual_change(before_full, after) >= 0.08
            ):
                data = _png_bytes(after)
                _press_escape()
                time.sleep(0.2)
                return data
            return _png_bytes(card)

        full = _grab_bbox(bounds)
        if full is None:
            return None
        pane_left = max(300, int(full.width * 0.35))
        pane_box = (pane_left, 70, full.width - 8, full.height - 120)
        if pane_box[2] <= pane_box[0] or pane_box[3] <= pane_box[1]:
            return None
        pane = full.crop(pane_box)
        rows = _ocr_rows(pane)
        target = quote_row(rows, body, sender, pane.height)
        if target is None:
            logger.debug("quoted image row was not found; returning chat pane")
            return _png_bytes(pane)

        _text, _x, row_y, _width, row_h = target
        card_box = (
            pane_box[0],
            max(pane_box[1], pane_box[1] + row_y - 300),
            pane_box[2],
            min(pane_box[3], pane_box[1] + row_y + row_h + 110),
        )
        card = full.crop(card_box)
        region = image_region(card, row_y)
        if region is None:
            return _png_bytes(card)

        x, y, width, height = region
        _click_relative(
            bounds[0],
            bounds[1],
            card_box[0] + x + width // 2,
            card_box[1] + y + height // 2,
        )
        time.sleep(0.65)
        after = _grab_bbox(bounds)
        if after is not None and _visual_change(full, after) >= 0.08:
            data = _png_bytes(after)
            _press_escape()
            time.sleep(0.2)
            return data
        return _png_bytes(card)
    except Exception as exc:
        logger.warning("quoted image capture failed: {}", type(exc).__name__)
        return None
    finally:
        if hwnd:
            restore_wechat_window(hwnd, previous)
