import unittest
from unittest.mock import patch

from wechat_oracle.wx_send_verify import _authored_row, normalize_text, text_matches


class SendVerificationTests(unittest.TestCase):
    def test_normalize_removes_wechat_mention_separator(self):
        self.assertEqual(
            normalize_text("@颜子壹\u2005收到"),
            "@颜子壹收到",
        )

    def test_short_text_requires_exact_match(self):
        self.assertTrue(text_matches("收到", "  收到  "))
        self.assertFalse(text_matches("收到", "没收到"))

    def test_long_text_accepts_minor_ocr_difference(self):
        expected = "这是需要确认发送成功的完整回复"
        actual = "这是需要确认发送成功的完整回覆"
        self.assertTrue(text_matches(expected, actual))

    @patch("wechat_oracle.wx_send_verify.settings.bot_wxid", "bot-id")
    def test_text_row_must_be_authored_by_bot_when_sender_is_known(self):
        row = {
            "sender_wxid": "someone-else",
            "content_text": "期望回复",
        }
        self.assertFalse(
            _authored_row(
                row,
                expected_text="期望回复",
                allow_any_outgoing=False,
                sender_key="sender_wxid",
            )
        )
        row["sender_wxid"] = "bot-id"
        self.assertTrue(
            _authored_row(
                row,
                expected_text="期望回复",
                allow_any_outgoing=False,
                sender_key="sender_wxid",
            )
        )

    @patch("wechat_oracle.wx_send_verify.settings.bot_wxid", "bot-id")
    def test_file_confirmation_accepts_any_new_bot_message(self):
        row = {"senderUsername": "bot-id", "content": "<msg></msg>"}
        self.assertTrue(
            _authored_row(
                row,
                expected_text=None,
                allow_any_outgoing=True,
                sender_key="senderUsername",
            )
        )


if __name__ == "__main__":
    unittest.main()
