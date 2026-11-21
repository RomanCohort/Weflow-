"""Regression for the real '7点准时提醒' misroute and evidence-only find output."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch

import test_class_actions as fixtures
from wechat_oracle import calendar_intent as ci,schedule as s
from wechat_oracle.agent.backends.native import NativeBackend
from wechat_oracle.agent.tools_control import ScheduleFollowupTool
from wechat_oracle.agent.tools import ToolError
from wechat_oracle.dispatcher import Candidate,FindCommand
from wechat_oracle.config import settings


class CalendarTrustTests(unittest.TestCase):
    def test_reminder_phrasing_is_write_but_queries_stay_read(self):
        self.assertTrue(ci.calendar_write_request('早上7点准时提醒 在体育场集合'))
        for text in ['查一下7点的提醒','看看有哪些已登记提醒','老师什么时候说的提醒']:
            self.assertFalse(ci.calendar_write_request(text))

    def test_find_rejects_invented_ids_and_keeps_actual_source(self):
        command=FindCommand(None,None,'集合通知')
        candidate=Candidate('m:12',int(datetime(2026,11,17,7,tzinfo=s.TZ).timestamp()),'老师','11月20日07:00在体育场集合。')
        missing=command._format_chat([candidate],['m:999'],'老师没有发通知')
        self.assertIn('未检索到',missing)
        self.assertNotIn('老师没有发通知',missing)
        found=command._format_chat([candidate],['m:12'],'模型自己猜的日期')
        for word in ('m:12','老师','2026','07:00','体育场'):self.assertIn(word,found)
        self.assertNotIn('模型自己猜',found)


class CalendarDBTests(unittest.TestCase):
    setUp=fixtures.ClassActionTests.setUp
    roles=fixtures.ClassActionTests.roles
    args=fixtures.ClassActionTests.args


def test_exact_time_route_creates_proposal_without_query_or_conversation_followup(self):
    item=self.args();item['starts_at']='2026-11-18T07:00:00+08:00'
    self.ctx.llm=type('LLM',(),{'complete_json':lambda _,**kw:json.dumps(item)})()
    with patch('wechat_oracle.agent.orchestrator.chat_via_agent',side_effect=AssertionError('no generic query route')):
        result=NativeBackend().chat(ctx=self.ctx,user_question='早上7点准时提醒 在体育场集合',trigger_kind='mention')
    self.assertIn('尚未执行',result.reply_text)
    plan=self.conn.execute('SELECT * FROM class_action_plans').fetchone()
    payload=json.loads(plan['payload'])
    self.assertTrue(payload['remind_at_start'])
    self.assertEqual(s.validate_event(payload,self.now)[2][1][1].hour,7)


def test_conversation_followup_cannot_schedule_calendar_request(self):
    self.conn.execute("ALTER TABLE messages ADD COLUMN content_text TEXT")
    self.conn.execute("UPDATE messages SET content_text='早上7点准时提醒在体育场集合' WHERE msg_id=1")
    tool=ScheduleFollowupTool(self.conn,'g','class','test-token',1,'mention',None,0,None)
    with self.assertRaises(ToolError):tool.call({'kind':'committed','intent':'7点提醒集合'})


def test_at_start_reminder_fires_instead_of_expiring(self):
    item=self.args();item.update(starts_at='2026-11-18T07:00:00+08:00',remind_at_start=True)
    data,start,reminders=s.validate_event(item,self.now)
    s.persist_event(self.ctx,data,start,reminders,'alice',self.now)
    sent=[]
    replier=type('Sender',(),{'send':lambda _,*args:sent.append(args)})()
    with patch.object(settings,'groups',['g']):
        s.deliver_due(self.conn,replier,start.timestamp()+3)
    self.assertEqual(len(sent),1)
    row=self.conn.execute("SELECT * FROM class_reminders WHERE label='当天'").fetchone()
    self.assertEqual(row['due_at'],start.timestamp())
    self.assertEqual(row['status'],'sent')
    self.assertIsNotNone(row['sent_at'])


CalendarDBTests.test_exact_time_route=test_exact_time_route_creates_proposal_without_query_or_conversation_followup
CalendarDBTests.test_no_conversation_calendar=test_conversation_followup_cannot_schedule_calendar_request
CalendarDBTests.test_at_start_reminder=test_at_start_reminder_fires_instead_of_expiring
