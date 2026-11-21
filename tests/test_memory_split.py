"""Regression tests for the 固定/近期 memory split and write_note."""
import sqlite3
import unittest

from wechat_oracle.agent import tools_write
from wechat_oracle.agent.memory import get_group_memory, upsert_group_memory
from wechat_oracle.agent.tools import ToolError
from wechat_oracle.config import settings


def _conn():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE group_memory(group_id TEXT PRIMARY KEY, notes_text TEXT,
                                  size_chars INTEGER, updated_at REAL, last_run_id INTEGER);
        CREATE TABLE persona_drift(group_id TEXT PRIMARY KEY, drift_text TEXT,
                                   updated_at REAL, last_run_id INTEGER);
        CREATE TABLE knowledge_documents(doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT, kind TEXT, owner TEXT, external_key TEXT,
            current_revision_id INTEGER, status TEXT, created_at REAL);
        CREATE TABLE knowledge_revisions(revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER, version INTEGER, title TEXT, course TEXT, body TEXT,
            digest TEXT, command_msg_id INTEGER, origin_msg_id INTEGER,
            origin_wx_id TEXT, quote_text TEXT, origin_text TEXT,
            previous_revision_id INTEGER, created_at REAL, published INTEGER DEFAULT 0);
        CREATE TABLE knowledge_chunks(chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER, ordinal INTEGER, text TEXT);
        CREATE TABLE knowledge_fts(text);
        CREATE TABLE knowledge_embedding_jobs(revision_id INTEGER PRIMARY KEY);
        """
    )
    return conn


class MemorySplitTests(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)
        self.gid = "g@chatroom"

    def _tool(self, text_on_disk):
        upsert_group_memory(self.conn, self.gid, text_on_disk)
        session = tools_write.MemoryWriteSession()
        session.group_memory_hash = tools_write._text_hash(get_group_memory(self.conn, self.gid))
        return tools_write.UpdateGroupMemoryTool(
            conn=self.conn, group_id=self.gid, session=session)

    def test_split_helper(self):
        ok, _ = tools_write._split_memory_sections("## 固定\nA\n## 近期\nB")
        self.assertEqual(ok, "A")
        self.assertIsNone(tools_write._split_memory_sections("## 近期\nB\n## 固定\nA")[0])
        self.assertIsNone(tools_write._split_memory_sections("no headers")[0])

    def test_rejects_missing_structure(self):
        tool = self._tool("legacy text")
        with self.assertRaises(ToolError) as ctx:
            tool.call({"notes_text": "still legacy"})
        self.assertIn("## 固定", str(ctx.exception))

    def test_rejects_over_budget_rolling(self):
        tool = self._tool("## 固定\nA\n## 近期\nB")
        big = "x" * (settings.agent_memory_rolling_chars + 10)
        with self.assertRaises(ToolError) as ctx:
            tool.call({"notes_text": "## 固定\nA\n## 近期\n" + big})
        self.assertIn("近期", str(ctx.exception))

    def test_accepts_within_budget(self):
        tool = self._tool("legacy")
        text = "## 固定\n- 父亲：颜子壹\n## 近期\n- 在聊华尔兹课"
        out = tool.call({"notes_text": text})
        self.assertIn("固定", out)
        self.assertEqual(get_group_memory(self.conn, self.gid), text)

    def test_write_note_creates_document_and_chunks(self):
        tool = tools_write.WriteNoteTool(conn=self.conn, group_id=self.gid, group_name="群")
        out = tool.call({"title": "领带事件", "body": "咕咕嘎嘎忘带领带，志睿帮忙找回。",
                         "slug": "tie-incident"})
        self.assertIn("note:tie-incident", out)
        doc = self.conn.execute(
            "select kind, external_key, status from knowledge_documents").fetchone()
        self.assertEqual(doc["kind"], "note")
        self.assertEqual(doc["status"], "active")
        chunks = self.conn.execute("select count(*) from knowledge_chunks").fetchone()[0]
        self.assertGreaterEqual(chunks, 1)
        jobs = self.conn.execute("select count(*) from knowledge_embedding_jobs").fetchone()[0]
        self.assertEqual(jobs, 1)

    def test_write_note_rejects_empty_and_oversize(self):
        tool = tools_write.WriteNoteTool(conn=self.conn, group_id=self.gid)
        with self.assertRaises(ToolError):
            tool.call({"title": "", "body": "x"})
        with self.assertRaises(ToolError):
            tool.call({"title": "t", "body": "x" * 1300})


if __name__ == "__main__":
    unittest.main()
