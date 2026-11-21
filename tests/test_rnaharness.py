"""Research gateway contract, duplicate/uncertain delivery, and group boundaries."""
import json
import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx

from wechat_oracle import rnaharness as r
from wechat_oracle.agent.backend import AgentChatOutcome
from wechat_oracle.agent.backends.native import NativeBackend
from wechat_oracle.config import settings,Settings
from wechat_oracle.dispatcher import CommandContext


class RNAHarnessTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:',isolation_level=None)
        self.conn.row_factory=sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,group_id TEXT,sender_wxid TEXT)')
        self.conn.executemany('INSERT INTO messages VALUES(?,?,?)',[(1,'g','a'),(2,'ai','b'),(3,'other','c')])
        r.init_rnaharness(self.conn)
        for key,value in dict(rnaharness_groups=['g','ai'],rnaharness_endpoint='http://127.0.0.1:8787',rnaharness_timeout_seconds=1,bot_wxid='bot').items():
            p=patch.object(settings,key,value);p.start();self.addCleanup(p.stop)
        p=patch.object(r.time,'sleep');p.start();self.addCleanup(p.stop)
        self.sent=[]
        self.ctx=CommandContext(self.conn,None,'fake','bot','g','test','Alice',20,20,None,
             trigger_msg_id=1,progress_sender=lambda text:self.sent.append(text) or True)
        self.requests=[];self.polls=0;self.events=[{'type':'answer.completed','task_id':'task-1','text':'真实结果'}]
        self.client=httpx.Client(transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)

    def handle(self,request):
        self.requests.append(request)
        if request.method=='POST':return httpx.Response(202,json={'task_id':'task-1'})
        self.polls+=1
        return httpx.Response(200,json={'events':self.events})

    def row(self):return self.conn.execute('SELECT * FROM rnaharness_requests ORDER BY request_id DESC LIMIT 1').fetchone()

    def run_chat(self):return r.chat(self.ctx,'分析这条RNA',client=self.client)

    def test_scope_and_existing_classroom_stay_local(self):
        self.assertTrue(r.should_route('g','mention','分析RNA'))
        for text in ['我想要AUAUUAUAUAUAU的结构，输出pdb','帮我折叠ACGUACGU',
                     '仅查看当前可用计算工具，不要提交计算任务','查询hsa-miR-21-5p与PTEN的文献证据',
                     'RNAHarness 帮我分析这段序列']:
            self.assertTrue(r.should_route('g','mention',text),text)
        for group,trigger,text in [('other','mention','分析RNA'),('g','probability','分析RNA'),('g','lurk','分析RNA'),
             ('g','mention','你好'),('g','reply','大家觉得这个方案怎么样？'),('g','mention','总结一下刚才的讨论'),
             ('g','mention','讲讲时间序列'),('g','mention','折叠屏好用吗'),
             ('g','mention','查本群待办'),('ai','reply','确认执行 P123'),('ai','mention','查询资料库')]:
            self.assertFalse(r.should_route(group,trigger,text))
        parsed=Settings(_env_file=None,rnaharness_groups='["g","ai"]')
        self.assertEqual(parsed.rnaharness_groups,['g','ai'])

    def test_success_ack_persist_and_duplicate_uses_actual_receipt(self):
        result=self.run_chat()
        self.assertEqual(result.reply_text,'真实结果')
        self.assertEqual(self.row()['state'],'completed')
        self.assertEqual(self.row()['ack_sent'],1)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.run_chat().reply_text,'真实结果')
        self.assertEqual(len(self.requests),2)
        body=json.loads(self.requests[0].content)
        self.assertTrue(body['chat_jid'].startswith('http:wechat-oracle:'))
        self.assertIn('Alice',body['content'])

    def test_same_group_session_and_cross_group_isolation(self):
        self.run_chat();one=json.loads(self.requests[0].content)['chat_jid']
        self.ctx.group_id='ai';self.ctx.group_name='AI';self.ctx.trigger_msg_id=2
        self.run_chat();two=json.loads(self.requests[2].content)['chat_jid']
        self.assertNotEqual(one,two)
        self.ctx.trigger_msg_id=1
        with self.assertRaisesRegex(ValueError,'当前群'):self.run_chat()
        self.ctx.group_id='other';self.ctx.trigger_msg_id=3
        with self.assertRaises(ValueError):self.run_chat()
        self.assertEqual(len(self.requests),4)

    def test_timeout_on_post_is_uncertain_and_never_resubmitted(self):
        def timeout(request):
            self.requests.append(request);raise httpx.ReadTimeout('timeout',request=request)
        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            result=r.chat(self.ctx,'分析RNA',client=client)
            self.assertIn('没有自动重新提交',result.reply_text)
            self.assertEqual(self.row()['state'],'uncertain')
            r.chat(self.ctx,'分析RNA',client=client)
        self.assertEqual(len(self.requests),1)

    def test_missing_old_task_is_not_silently_resubmitted(self):
        row,_=r.prepare(self.ctx,'分析RNA')
        r.update(self.conn,row['request_id'],'running',task='old-task')
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(404))) as client:
            result=r.chat(self.ctx,'分析RNA',client=client)
        self.assertIn('TASK_UNAVAILABLE',result.reply_text)
        self.assertEqual(self.row()['state'],'uncertain')

    def test_kernel_failure_and_wrong_task_never_become_results(self):
        self.events=[{'type':'task.failed','task_id':'task-1','message':'secret internal error'},
                     {'type':'answer.completed','task_id':'task-1','text':'invented success'}]
        result=self.run_chat()
        self.assertIn('KERNEL_FAILED',result.reply_text)
        self.assertNotIn('secret',result.reply_text)
        self.conn.execute('DELETE FROM rnaharness_requests')
        self.events=[{'type':'answer.completed','task_id':'other-task','text':'OTHER_GROUP_SECRET'}]
        result=self.run_chat()
        self.assertIn('INVALID_EVENTS',result.reply_text)
        self.assertNotIn('OTHER_GROUP_SECRET',result.reply_text)

    def test_specific_kernel_failures_are_mapped_without_leaking_details(self):
        for message,code in [
            ('续写失败：TimeoutError: timed out','KERNEL_TIMEOUT'),
            ('输出在 3 次续写后仍被截断','OUTPUT_TRUNCATED'),
            ('Occamy output failed the degeneration sentinel','DEGENERATE_OUTPUT'),
            ('secret internal error','KERNEL_FAILED')]:
            self.conn.execute('DELETE FROM rnaharness_requests')
            self.events=[{'type':'task.failed','task_id':'task-1','message':message}]
            with self.subTest(code=code):
                result=self.run_chat()
                self.assertIn(code,result.reply_text)
                self.assertNotIn('secret',result.reply_text)

    def test_degenerate_quote_and_result_are_contained(self):
        self.ctx.quoted_text='?'*5000
        self.events[0]['text']='?'*5000
        result=self.run_chat()
        self.assertIn('DEGENERATE_OUTPUT',result.reply_text)
        self.assertNotIn('?'*100,result.reply_text)
        body=json.loads(self.requests[0].content)['content']
        self.assertIn('已过滤异常重复字符',body)
        self.assertNotIn('?'*100,body)
        self.conn.execute('DELETE FROM rnaharness_requests')
        self.ctx.quoted_text='正常引用'
        self.events[0]['text']='真实结果'
        self.assertEqual(self.run_chat().reply_text,'真实结果')

    def test_real_tool_progress_only_and_no_raw_deltas(self):
        def handle(request):
            if request.method=='POST':return httpx.Response(202,json={'task_id':'task-1'})
            self.polls+=1
            events=self.events if self.polls>1 else [
                {'type':'tool.started','task_id':'task-1','tool_name':'rna_fold'},
                {'type':'answer.delta','task_id':'task-1','text':'unverified fragment'}]
            return httpx.Response(200,json={'events':events})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            result=r.chat(self.ctx,'分析RNA',client=client)
        self.assertEqual(result.reply_text,'真实结果')
        self.assertEqual(self.row()['progress_sent'],1)
        self.assertIn('rna_fold',self.sent[-1])
        self.assertFalse(any('unverified' in x for x in self.sent))

    def test_ack_failure_does_not_discard_result(self):
        self.ctx.progress_sender=lambda text:False
        self.assertEqual(self.run_chat().reply_text,'真实结果')
        self.assertEqual(self.row()['ack_sent'],0)

    def test_invalid_endpoint_and_empty_result(self):
        for url in ('https://example.org','http://127.0.0.1:8787/path','http://user:password@127.0.0.1:8787'):
            with patch.object(settings,'rnaharness_endpoint',url):
                with self.assertRaises(ValueError):self.run_chat()
        self.assertEqual(len(self.requests),0)
        self.events[0]['text']=' '
        self.assertIn('EMPTY_RESULT',self.run_chat().reply_text)

    def test_full_result_is_stored_when_display_is_bounded(self):
        self.events[0]['text']='原文'*4000
        result=self.run_chat()
        self.assertIn('已截断',result.reply_text)
        self.assertEqual(len(self.row()['reply_text']),8000)

    def test_native_routes_only_selected_direct_turns(self):
        with patch('wechat_oracle.class_actions.confirmation',return_value=None), \
             patch('wechat_oracle.class_actions.denied_management_request',return_value=None), \
             patch('wechat_oracle.calendar_intent.calendar_entry',return_value=None), \
             patch('wechat_oracle.rnaharness.chat',return_value=AgentChatOutcome('RNA结果','trace')) as remote, \
             patch('wechat_oracle.agent.orchestrator.chat_via_agent',return_value=AgentChatOutcome('本地结果','trace')):
            self.assertEqual(NativeBackend().chat(ctx=self.ctx,user_question='分析RNA',trigger_kind='mention').reply_text,'RNA结果')
            self.assertEqual(NativeBackend().chat(ctx=self.ctx,user_question='分析RNA',trigger_kind='probability').reply_text,'本地结果')
            self.ctx.group_id='other'
            self.assertEqual(NativeBackend().chat(ctx=self.ctx,user_question='你好',trigger_kind='mention').reply_text,'本地结果')
            self.assertEqual(remote.call_count,1)
