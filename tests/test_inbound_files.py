import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_oracle.agent.inbound_files import find_inbound_files


class InboundFilesTests(unittest.TestCase):
    def test_finds_file_named_in_current_group_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "wxid_test" / "msg" / "file" / "2026-11"
            cache.mkdir(parents=True)
            (cache / "probe.txt").write_bytes(b"hello")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "create table messages (msg_id integer, group_id text, type text, content_text text, media_path text)"
            )
            conn.executemany(
                "insert into messages values (?,?,?,?,?)",
                [
                    (1, "g1@chatroom", "file", "[文件] probe.txt", None),
                    (2, "g2@chatroom", "file", "[文件] secret.txt", None),
                ],
            )
            with patch("wechat_oracle.agent.inbound_files._candidate_roots", return_value=[root]):
                found = find_inbound_files(conn, "g1@chatroom")
            self.assertEqual([item["name"] for item in found], ["probe.txt"])
            self.assertEqual(found[0]["size"], 5)


if __name__ == "__main__":
    unittest.main()
