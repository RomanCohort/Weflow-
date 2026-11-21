import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from wechat_oracle.replier import ReplySendError, Wx4pyReplier


class Wx4pyReplierTests(unittest.TestCase):
    def setUp(self):
        self.chat = Mock()
        self.chat.send_to.return_value = True
        self.chat.send_file_to.return_value = True
        self.wx = Mock(chat_window=self.chat)
        self.wx.window.hwnd = 101
        self.replier = Wx4pyReplier(self.wx)
        self.baseline_patcher = patch(
            "wechat_oracle.replier.latest_message_id", return_value=10
        )
        self.confirm_patcher = patch(
            "wechat_oracle.replier.confirm_send_recorded", return_value=True
        )
        self.bring_patcher = patch(
            "wechat_oracle.wx_preview_capture.bring_wechat_to_front",
            return_value=202,
        )
        self.restore_patcher = patch(
            "wechat_oracle.wx_preview_capture.restore_wechat_window"
        )
        self.baseline = self.baseline_patcher.start()
        self.confirm = self.confirm_patcher.start()
        self.bring = self.bring_patcher.start()
        self.restore = self.restore_patcher.start()

    def tearDown(self):
        self.restore_patcher.stop()
        self.bring_patcher.stop()
        self.confirm_patcher.stop()
        self.baseline_patcher.stop()

    def test_send_uses_official_send_to(self):
        self.replier.send(
            "群", None, "正文", target="群", target_id="g@chatroom"
        )
        self.chat.send_to.assert_called_once_with(
            "群", "正文", target_type="group"
        )
        self.bring.assert_called_once_with(101)
        self.restore.assert_called_once_with(101, 202)
        self.confirm.assert_called_once()

    def test_failed_send_is_not_retried(self):
        self.chat.send_to.return_value = False
        with self.assertRaisesRegex(ReplySendError, "returned false"):
            self.replier.send(
                "群", None, "正文", target="群", target_id="g@chatroom"
            )
        self.chat.send_to.assert_called_once()
        self.confirm.assert_not_called()

    def test_unconfirmed_send_is_reported(self):
        self.confirm.return_value = False
        with self.assertRaisesRegex(ReplySendError, "not confirmed"):
            self.replier.send(
                "群", None, "正文", target="群", target_id="g@chatroom"
            )
        self.chat.send_to.assert_called_once()

    def test_file_send_uses_official_send_file_to(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "result.txt"
            path.write_text("ok", encoding="utf-8")
            self.replier.send_file(
                "群", path, "说明", target_id="g@chatroom"
            )
        self.chat.send_file_to.assert_called_once_with(
            "群",
            str(path),
            target_type="group",
            message="说明",
        )
        self.confirm.assert_called_once()

    def test_capture_preview_delegates_to_screenshot_capture(self):
        with patch(
            "wechat_oracle.wx_preview_capture.capture_quote_preview",
            return_value=b"png",
        ) as capture:
            result = self.replier.capture_image_preview(
                "g@chatroom", "群", "引用正文", "发送者", 123.0
            )
        self.assertEqual(result, b"png")
        capture.assert_called_once_with(
            self.wx, "g@chatroom", "群", "引用正文", "发送者", 123.0
        )

    def test_disconnect_uses_official_client(self):
        self.replier.disconnect()
        self.wx.disconnect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
