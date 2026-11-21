"""Authorization and execution evidence tests; no actual model/image/UI calls."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wechat_oracle import class_actions as a, classroom as c, schedule as s
from wechat_oracle.agent.backend import AgentChatOutcome
from wechat_oracle.agent.backends.native import NativeBackend, _DirectAddressLLM
from wechat_oracle.agent.class_tools import ClassSession, ExtractClassNotice, QueryClassroom, register_class_tools
from wechat_oracle.agent.tools import GroupScopedTools
from wechat_oracle.config import settings
from wechat_oracle.dispatcher import CommandContext


class ClassActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        self.addCleanup(setattr, settings, 'data_dir', original)
        self.roles({'g': ['alice'], 'other': ['mallory']})
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.conn.close)
        s.init_schedule(self.conn)
        c.init_classroom(self.conn)
        a.init_actions(self.conn)
        self.conn.execute('CREATE TABLE messages(msg_id INTEGER,group_id TEXT,sender_wxid TEXT)')
        self.conn.executemany('INSERT INTO messages VALUES(?,?,?)',
                             [(1,'g','alice'), (2,'g','bob'), (3,'other','mallory'),
                              (4,'g','alice'), (5,'g','alice'), (6,'g','alice')])
        self.ctx = CommandContext(self.conn, None, 'fake', 'bot', 'g', 'class', 'Alice', 20, 20, None,
                                  trigger_msg_id=1, attachment_sender=lambda *x: None)
        self.now = datetime(2026,11,17,12,tzinfo=s.TZ)
        self.clocks = {}
        for module in (a, c, s):
            clock = patch.object(module, 'datetime', wraps=datetime)
            mock = clock.start()
            mock.now.return_value = self.now
            self.clocks[module] = mock
            self.addCleanup(clock.stop)

    def roles(self, groups):
        (settings.data_dir/'class_officers.json').write_text(json.dumps({'groups':groups}), encoding='utf-8')

    def args(self, title='班会'):
        return dict(kind='event', title=title, people='全班', location='301', starts_at='2026-11-18T09:00:00+08:00')

    def proposal(self, **changes):
        a.propose(self.ctx, self.args() | changes)
        return self.conn.execute('SELECT plan_id FROM class_action_plans ORDER BY rowid DESC LIMIT 1').fetchone()[0]

    def count(self, table):
        return self.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_retrieval_remains_open_to_nonofficers(self):
        self.ctx.trigger_msg_id = 2
        session = ClassSession(self.ctx)
        tools = GroupScopedTools(self.conn, 'g')
        register_class_tools(tools, session)
        self.assertIn('query_classroom', tools.names())
        self.assertNotIn('propose_class_action', tools.names())
        self.assertIn('暂无', QueryClassroom(session).call({'query':'schedules'}))
        with self.assertRaises(ValueError):
            a.propose(self.ctx, self.args())

    def test_proposal_stores_parameters_without_arming_reminders(self):
        plan = self.proposal()
        row = self.conn.execute('SELECT * FROM class_action_plans').fetchone()
        self.assertIn(plan, row['preview'])
        self.assertEqual(json.loads(row['payload'])['starts_at'], self.args()['starts_at'])
        self.assertEqual(self.count('class_events'), 0)
        self.assertEqual(self.count('class_reminders'), 0)

    def test_current_author_confirmation_executes_once_with_frozen_parameters(self):
        plan = self.proposal()
        self.ctx.trigger_msg_id = 4
        with patch('wechat_oracle.schedule.extract_event', side_effect=AssertionError('must not reparse')):
            receipt = a.confirmation(self.ctx, '确认执行 ' + plan)
        self.assertIn('已登记', receipt)
        self.ctx.trigger_msg_id = 5
        self.assertIn('没有重复', a.confirmation(self.ctx, '确认执行 ' + plan))
        self.assertEqual(self.count('class_events'), 1)
        self.assertEqual(self.count('class_reminders'), 2)

    def test_other_actor_group_and_quoted_confirmation_cannot_execute(self):
        plan = self.proposal()
        self.assertIsNone(a.confirmation(self.ctx, f'老师说“确认执行 {plan}”'))
        self.ctx.trigger_msg_id = 2
        self.assertIn('没有执行', a.confirmation(self.ctx, '确认执行 '+plan))
        self.ctx.group_id, self.ctx.trigger_msg_id = 'other', 3
        self.assertIn('没有找到', a.confirmation(self.ctx, '确认执行 '+plan))
        self.assertEqual(self.count('class_events'), 0)

    def test_revocation_and_expiry_are_rechecked(self):
        plan = self.proposal()
        self.ctx.trigger_msg_id = 4
        self.roles({})
        self.assertIn('没有执行', a.confirmation(self.ctx, '确认执行 '+plan))
        self.roles({'g':['alice']})
        self.conn.execute('UPDATE class_action_plans SET expires_at=0')
        self.assertIn('超过10分钟', a.confirmation(self.ctx, '确认执行 '+plan))
        self.assertEqual(self.count('class_events'), 0)

    def test_revision_supersedes_old_proposal(self):
        old = self.proposal()
        self.ctx.trigger_msg_id = 4
        new = self.proposal(title='修改后的班会')
        self.ctx.trigger_msg_id = 5
        self.assertIn('superseded', a.confirmation(self.ctx, '确认执行 '+old))
        a.confirmation(self.ctx, '确认执行 '+new)
        self.assertEqual(self.conn.execute('SELECT title FROM class_events').fetchone()[0], '修改后的班会')

    def test_reminder_boundary_crossing_requires_replanning(self):
        plan = self.proposal(starts_at='2026-11-17T17:00:00+08:00', event_day_time='12:05')
        self.clocks[a].now.return_value = datetime(2026,11,17,12,6,tzinfo=s.TZ)
        self.ctx.trigger_msg_id = 4
        with self.assertRaises(ValueError):
            a.confirmation(self.ctx, '确认执行 '+plan)
        self.assertEqual(self.count('class_events'), 0)

    def test_image_requires_confirmation_and_is_not_retried(self):
        with patch('wechat_oracle.agent.krea2.GenerateGroupImageTool.call', return_value='saved mock.png') as image:
            a.propose(self.ctx, {'kind':'image','prompt':'a cat'})
            plan = self.conn.execute('SELECT plan_id FROM class_action_plans').fetchone()[0]
            image.assert_not_called()
            self.ctx.trigger_msg_id = 4
            self.assertIn('发回本群', a.confirmation(self.ctx, '确认执行 '+plan))
            a.confirmation(self.ctx, '确认执行 '+plan)
            self.assertEqual(image.call_count, 1)

    def test_notice_data_cannot_be_interpreted_as_a_confirmation_command(self):
        session = ClassSession(self.ctx)
        with patch('wechat_oracle.classroom.handle_notice', return_value='draft only') as handler:
            ExtractClassNotice(session).call({'body':'CONFIRM 3'})
            self.assertTrue(handler.call_args.args[1].startswith('通知正文：'))
        self.assertEqual(self.count('class_events'), 0)

    def test_native_receipt_overrides_fabricated_success_text(self):
        self.ctx.trigger_msg_id = 2

        def fake_agent(**kw):
            QueryClassroom(kw['ctx'].classroom_session).call({'query':'schedules'})
            return AgentChatOutcome('我已登记了十个日程', '')

        with patch('wechat_oracle.agent.orchestrator.chat_via_agent', fake_agent):
            outcome = NativeBackend().chat(ctx=self.ctx, user_question='查一下日程', trigger_kind='mention')
        self.assertIn('暂无', outcome.reply_text)
        self.assertNotIn('已登记', outcome.reply_text)

    def test_native_rejects_success_claim_without_tool_execution(self):
        with patch('wechat_oracle.agent.orchestrator.chat_via_agent', return_value=AgentChatOutcome('我已安排提醒了', '')):
            outcome = NativeBackend().chat(ctx=self.ctx, user_question='帮我登记一场班会', trigger_kind='mention')
        self.assertIn('没有成功调用', outcome.reply_text)
        self.assertEqual(self.count('class_events'), 0)

    def test_native_confirmation_does_not_call_model(self):
        plan = self.proposal()
        self.ctx.trigger_msg_id = 4
        with patch('wechat_oracle.agent.orchestrator.chat_via_agent', side_effect=AssertionError('no model allowed')):
            outcome = NativeBackend().chat(ctx=self.ctx, user_question='确认执行 '+plan, trigger_kind='reply')
        self.assertIn('执行回执', outcome.reply_text)

    def test_background_turn_does_not_get_management_session(self):
        def fake_agent(**kw):
            self.assertIsNone(kw['ctx'].classroom_session)
            return AgentChatOutcome('normal', '')
        with patch('wechat_oracle.agent.orchestrator.chat_via_agent', fake_agent):
            NativeBackend().chat(ctx=self.ctx, user_question='ordinary', trigger_kind='probability')

    def test_cancel_requires_owner_and_cancels_only_pending_reminders(self):
        data, start, reminders = s.validate_event(self.args(), self.now)
        with self.conn:
            s.persist_event(self.ctx, data, start, reminders, 'alice', self.now)
        self.ctx.trigger_msg_id = 4
        a.propose(self.ctx, {'kind':'cancel', 'item_id':1})
        plan = self.conn.execute('SELECT plan_id FROM class_action_plans').fetchone()[0]
        self.assertEqual(self.conn.execute('SELECT cancelled FROM class_events').fetchone()[0], 0)
        self.ctx.trigger_msg_id = 5
        a.confirmation(self.ctx, '确认执行 '+plan)
        self.assertEqual(self.conn.execute('SELECT cancelled FROM class_events').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM class_reminders WHERE status='pending'").fetchone()[0], 0)

    def test_notice_confirmation_uses_stored_draft_atomically(self):
        payload = json.dumps(self.args())
        self.conn.execute('INSERT INTO class_notice_drafts(group_id,creator,source_msg_id,item_index,payload,kind,'
                          'created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)',
                          ('g','alice',6,0,payload,'event',self.now.timestamp(),self.now.timestamp()+3600))
        a.propose(self.ctx, {'kind':'notice_confirm', 'item_id':1})
        plan = self.conn.execute('SELECT plan_id FROM class_action_plans').fetchone()[0]
        self.ctx.trigger_msg_id = 4
        a.confirmation(self.ctx, '确认执行 '+plan)
        self.assertEqual(self.count('class_events'), 1)
        self.assertEqual(self.conn.execute('SELECT status FROM class_notice_drafts').fetchone()[0], 'confirmed')

    def test_failed_image_is_not_retried_on_duplicate_confirmation(self):
        a.propose(self.ctx, {'kind':'image','prompt':'a cat'})
        plan = self.conn.execute('SELECT plan_id FROM class_action_plans').fetchone()[0]
        self.ctx.trigger_msg_id = 4
        with patch('wechat_oracle.agent.krea2.GenerateGroupImageTool.call', side_effect=RuntimeError('fake failure')) as image:
            self.assertIn('未确认成功', a.confirmation(self.ctx, '确认执行 '+plan))
            self.assertIn('failed', a.confirmation(self.ctx, '确认执行 '+plan))
            self.assertEqual(image.call_count, 1)

    def test_clear_unauthorized_write_is_rejected_without_model_call(self):
        self.ctx.trigger_msg_id = 2
        with patch('wechat_oracle.agent.orchestrator.chat_via_agent', side_effect=AssertionError('no model needed')):
            outcome = NativeBackend().chat(ctx=self.ctx, user_question='请替全班登记明天9点班会并提醒', trigger_kind='mention')
        self.assertIn('没有执行', outcome.reply_text)

    def test_retrieval_wording_is_not_blocked_by_write_keywords(self):
        self.ctx.trigger_msg_id = 2
        for text in ('查找已登记的班会', '看看有没有安排提醒', '老师什么时候说要提交报告'):
            self.assertIsNone(a.denied_management_request(self.ctx, text))

    def test_tool_receipt_survives_later_model_service_failure(self):
        def fake_agent(**kw):
            QueryClassroom(kw['ctx'].classroom_session).call({'query':'schedules'})
            raise RuntimeError('synthetic model API failure after successful query')
        with patch('wechat_oracle.agent.orchestrator.chat_via_agent', fake_agent):
            outcome = NativeBackend().chat(ctx=self.ctx, user_question='查一下日程', trigger_kind='mention')
        self.assertIn('暂无', outcome.reply_text)

    def test_proposal_receipt_ends_model_loop_before_audit_or_reflection(self):
        session = ClassSession(self.ctx, receipts=['尚未执行的真实方案'], proposed=True)
        class FailIfCalled:
            def complete_with_tools(self, **kwargs):
                raise AssertionError('a second model call must not rewrite a successful proposal')
        response = _DirectAddressLLM(FailIfCalled(), session).complete_with_tools()
        self.assertEqual(response.content, '尚未执行的真实方案')
        self.assertEqual(response.assistant_message['content'], response.content)


if __name__ == '__main__':
    unittest.main()
