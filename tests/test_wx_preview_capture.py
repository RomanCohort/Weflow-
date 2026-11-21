import unittest

from PIL import Image

from wechat_oracle.wx_preview_capture import (
    MAX_PREVIEW_BYTES,
    _find_refer_image_rect,
    body_fragment,
    quote_row,
    _png_bytes,
)


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _Control:
    def __init__(self, name, class_name, rect, children=()):
        self.Name = name
        self.ClassName = class_name
        self.BoundingRectangle = rect
        self._children = list(children)

    def GetChildren(self):
        return self._children


class PreviewCaptureTests(unittest.TestCase):
    def test_body_fragment_prefers_last_nonempty_word(self):
        self.assertEqual(body_fragment("@某人 看看这张图"), "看看这张图")

    def test_quote_row_prefers_body_over_sender_only_match(self):
        rows = [
            ("某人", 0, 100, 20, 20),
            ("这是引用正文", 0, 300, 100, 20),
            ("某人", 0, 500, 20, 20),
        ]
        self.assertEqual(
            quote_row(rows, "这是引用正文", "某人", 700),
            ("这是引用正文", 0, 300, 100, 20),
        )

    def test_png_output_stays_under_storage_limit(self):
        image = Image.new("RGB", (2400, 1600), "white")
        data = _png_bytes(image)
        self.assertIsNotNone(data)
        self.assertLessEqual(len(data), MAX_PREVIEW_BYTES)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_uia_finds_refer_image_before_matching_quote_text(self):
        text = _Control(
            "@某人 你能看到图吗 引用 某人 的消息：图片",
            "mmui::ChatTextItemView",
            _Rect(444, 631, 1407, 756),
        )
        image = _Control(
            "图片",
            "mmui::ChatBubbleReferItemView",
            _Rect(444, 343, 1407, 631),
        )
        root = _Control("", "mmui::MainWindow", _Rect(0, 0, 1407, 888), [image, text])
        self.assertEqual(
            _find_refer_image_rect(root, "你能看到图吗", "某人"),
            (444, 343, 1407, 631),
        )


if __name__ == "__main__":
    unittest.main()
