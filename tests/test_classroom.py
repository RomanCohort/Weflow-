"""Classroom authorization, confirmation, persistence, and reminder regressions.

Run with PYTHONPATH=src python -m unittest discover -s tests.
No model, WeChat, network, production DB, or external sends are used.
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_oracle import classroom as c, schedule as s
from wechat_oracle.config import settings


class FakeLLM:
    def __init__(self):
        self.payload = None
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.payload)


class ClassroomTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        s.init_schedule(self.conn)
        c.init_classroom(self.conn)
        self.conn.execute('CREATE TABLE messages(msg_id INTEGER,group_id TEXT,sender_wxid TEXT)')
        self.conn.executemany('INSERT INTO messages VALUES(?,?,?)',
                             [(1, 'g', 'alice'), (2, 'g', 'bob'), (3, 'other', 'mallory'),
                              (4, 'g', 'alice'), (5, 'g', 'alice'), (6, 'g', 'alice')])
        self.llm = FakeLLM()
        self.ctx = SimpleNamespace(conn=self.conn, llm=self.llm, model='fake', group_id='g',
                                   group_name='班级群', trigger_msg_id=1, requester='Alice', quoted_text='')
        self.now = datetime(2026, 11, 17, 12, tzinfo=s.TZ)
        for module in (s, c):
            clock = patch.object(module, 'datetime', wraps=datetime)
            mock = clock.start()
            mock.now.return_value = self.now
            self.addCleanup(clock.stop)
        self.old_groups = settings.groups
        settings.groups = []
        self.addCleanup(setattr, settings, 'groups', self.old_groups)
        self.addCleanup(self.conn.close)

    def item(self, *, kind='event', title='班会', at='2026-11-18T09:00:00+08:00'):
        return dict(kind=kind, title=title, starts_at=at, people='全班', location='301', clarification='')

    def notice(self, items=None):
        self.llm.payload = dict(items=items or [self.item()], clarification='')
        return c.handle_notice(self.ctx, '明天9点班会')

    def count(self, table):
        return self.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_notice_draft_has_no_events_or_reminders_and_quote_is_input(self):
        self.ctx.quoted_text = '老师原通知：明天9点在301开班会'
        text = self.notice()
        self.assertIn('尚未安排提醒', text)
        self.assertIn(self.ctx.quoted_text, self.llm.calls[0]['user'])
        self.assertEqual(self.count('class_events'), 0)
        self.assertEqual(self.count('class_reminders'), 0)
        c.handle_notice(self.ctx, '重复处理同一条消息')
        self.assertEqual(self.count('class_notice_drafts'), 1)
        self.assertEqual(len(self.llm.calls), 1)

    def test_only_author_in_original_group_can_confirm(self):
        self.notice()
        for group, msg in [('g', 2), ('other', 3)]:
            self.ctx.group_id, self.ctx.trigger_msg_id = group, msg
            with self.assertRaises(ValueError):
                c.handle_notice(self.ctx, 'confirm 1')
        self.assertEqual(self.count('class_events'), 0)
        self.ctx.group_id, self.ctx.trigger_msg_id = 'g', 4
        self.assertIn('已登记', c.handle_notice(self.ctx, 'confirm 1'))
        self.ctx.trigger_msg_id = 5
        self.assertIn('没有重复创建', c.handle_notice(self.ctx, 'confirm 1'))
        self.assertEqual(self.count('class_events'), 1)
        self.assertEqual(self.count('class_reminders'), 2)

    def test_multi_notice_items_confirm_individually(self):
        self.notice([self.item(), self.item(kind='todo', title='交报告')])
        self.assertEqual(self.count('class_notice_drafts'), 2)
        for draft, msg in [(1, 4), (2, 5)]:
            self.ctx.trigger_msg_id = msg
            c.handle_notice(self.ctx, f'confirm {draft}')
        kinds = [r[0] for r in self.conn.execute('SELECT kind FROM class_events ORDER BY event_id')]
        self.assertEqual(kinds, ['event', 'todo'])

    def test_failure_rolls_back_confirmation_and_event(self):
        self.notice()
        self.ctx.trigger_msg_id = 4
        real = c.persist_event

        def fail_after_insert(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('simulated interruption before commit')

        with patch.object(c, 'persist_event', fail_after_insert):
            with self.assertRaises(RuntimeError):
                c.handle_notice(self.ctx, 'confirm 1')
        self.assertEqual(self.count('class_events'), 0)
        self.assertEqual(self.count('class_reminders'), 0)
        self.assertEqual(self.conn.execute('SELECT status FROM class_notice_drafts').fetchone()[0], 'pending')

    def test_expired_or_dismissed_draft_does_not_schedule(self):
        self.notice()
        self.ctx.trigger_msg_id = 4
        self.conn.execute('UPDATE class_notice_drafts SET expires_at=0')
        with self.assertRaises(ValueError):
            c.handle_notice(self.ctx, 'confirm 1')
        self.assertEqual(self.count('class_events'), 0)

    def test_todo_self_reports_do_not_modify_other_members(self):
        self.llm.payload = self.item(kind='todo', title='实验报告', at='2026-11-17T17:00:00+08:00')
        c.handle_todo(self.ctx, '今天17点前交实验报告')
        c.handle_todo(self.ctx, 'done 1')
        self.ctx.trigger_msg_id, self.ctx.requester = 2, 'Bob'
        c.handle_todo(self.ctx, 'done 1')
        c.handle_todo(self.ctx, 'undo 1')
        reports = dict(self.conn.execute('SELECT member_wxid,done FROM class_todo_reports'))
        self.assertEqual(reports, {'alice': 1, 'bob': 0})
        self.assertIn('未确认者不等于未完成', c.handle_todo(self.ctx, 'status 1'))
        self.assertIn('未找到', c.handle_todo(self.ctx, 'cancel 1'))
        self.ctx.group_id, self.ctx.trigger_msg_id = 'other', 3
        with self.assertRaises(ValueError):
            c.handle_todo(self.ctx, 'done 1')

    def test_dashboard_has_no_drafts_or_other_group_items_and_needs_no_llm(self):
        self.notice([self.item(title='未确认秘密草稿')])
        self.ctx.trigger_msg_id = 4
        self.llm.payload = self.item(kind='todo', title='实验报告', at='2026-11-17T17:00:00+08:00')
        c.handle_todo(self.ctx, '今天17点交报告')
        self.ctx.group_id, self.ctx.group_name, self.ctx.trigger_msg_id = 'other', '别的群', 3
        self.llm.payload = self.item(title='跨群不应出现', at='2026-11-17T16:00:00+08:00')
        s.handle_schedule(self.ctx, '别的群活动')
        self.ctx.group_id = 'g'
        before = len(self.llm.calls)
        for week in (False, True):
            board = c.dashboard(self.ctx, week=week)
            self.assertIn('实验报告', board)
            self.assertNotIn('未确认秘密草稿', board)
            self.assertNotIn('跨群不应出现', board)
        self.assertEqual(len(self.llm.calls), before)

    def test_todo_reminder_is_automatic_and_not_repeated(self):
        self.llm.payload = self.item(kind='todo', title='实验报告', at='2026-11-17T17:00:00+08:00')
        c.handle_todo(self.ctx, '今天17点交报告')
        sent = []
        replier = SimpleNamespace(send=lambda *args: sent.append(args))
        for _ in range(2):
            s.deliver_due(self.conn, replier, self.now.timestamp())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], '班级群')
        self.assertIn('待办截止提醒', sent[0][2])
        self.assertIn('截止：', sent[0][2])

    def test_bad_or_ambiguous_model_result_never_creates_drafts(self):
        for payload in ({'clarification': '几点截止？'}, {'items': 'bad'},
                        {'items': [self.item(at='2026-11-18T09:00:00')]}):
            self.llm.payload = payload
            try:
                c.handle_notice(self.ctx, '周五交作业')
            except ValueError:
                pass
        self.assertEqual(self.count('class_notice_drafts'), 0)

    def test_pending_reminders_survive_reopening_database(self):
        self.llm.payload = self.item()
        s.handle_schedule(self.ctx, '明天9点开会')
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'test.db'
            with closing(sqlite3.connect(db)) as disk:
                self.conn.backup(disk)
            with closing(sqlite3.connect(db, isolation_level=None)) as reopened:
                reopened.row_factory = sqlite3.Row
                s.init_schedule(reopened)
                c.init_classroom(reopened)
                self.assertEqual(reopened.execute('SELECT count(*) FROM class_events').fetchone()[0], 1)
                self.assertEqual(reopened.execute('SELECT count(*) FROM class_reminders').fetchone()[0], 2)

    def test_command_registration_and_argument_validation(self):
        from wechat_oracle.dispatcher import COMMANDS, ParseError, _try_parse_slash
        for name in ('notice', 'todo', 'today', 'week', 'schedule', 'krea'):
            self.assertIn(name, COMMANDS)
        self.assertEqual(_try_parse_slash('/notice').name, 'notice')
        self.assertIsInstance(COMMANDS['today'].parse('wrong'), ParseError)

    def test_legacy_schema_migration_preserves_existing_events(self):
        with closing(sqlite3.connect(':memory:', isolation_level=None)) as old:
            old.row_factory = sqlite3.Row
            old.execute("CREATE TABLE class_events(event_id INTEGER PRIMARY KEY,group_id TEXT NOT NULL,"
                        "group_name TEXT NOT NULL,creator TEXT NOT NULL,source_msg_id INTEGER NOT NULL,"
                        "title TEXT NOT NULL,people TEXT NOT NULL,location TEXT NOT NULL,starts_at REAL NOT NULL,"
                        "cancelled INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,UNIQUE(group_id,source_msg_id))")
            old.execute("INSERT INTO class_events VALUES(1,'g','class','alice',1,'keep','all','301',1,0,1)")
            s.init_schedule(old)
            c.init_classroom(old)
            s.init_schedule(old)
            row = old.execute('SELECT * FROM class_events').fetchone()
            self.assertEqual((row['title'], row['kind']), ('keep', 'event'))


if __name__ == '__main__':
    unittest.main()
