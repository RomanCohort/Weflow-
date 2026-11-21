"""Provenance, revision, retrieval isolation and embedding failure regressions."""
import array
import json
import tempfile
import unittest
import uuid
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from loguru import logger

from wechat_oracle import classroom as c,knowledge as k,knowledge_search as ks,knowledge_views as v,schedule as s
from wechat_oracle import class_actions as a
from wechat_oracle.config import settings
from wechat_oracle.db import _connect,init_db,transaction
from wechat_oracle.dispatcher import CommandContext
from wechat_oracle.ingest.writer import write_messages
from wechat_oracle.models import Message,MsgType


class FakeEmbeddings:
    def __init__(self,identity='fake-v1'):self.model_id=identity
    def identity(self):return self.model_id
    def encode(self,texts,query=False):return [ks.normalized([1.0,0.0]) for _ in texts]


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        keys=('data_dir','media_dir','agent_output_dir','db_path','groups','embeddings_enabled','embeddings_model')
        old={name:getattr(settings,name) for name in keys}
        self.addCleanup(lambda:[setattr(settings,name,value) for name,value in old.items()])
        root=Path(self.temp.name)
        settings.data_dir=root;settings.media_dir=root/'media';settings.agent_output_dir=root/'out'
        settings.db_path=root/'db.sqlite';settings.groups=[];settings.embeddings_enabled=False
        (root/'class_officers.json').write_text(json.dumps({'groups':{'g':['alice'],'other':['mallory']}}))
        init_db(settings.db_path);self.conn=_connect(settings.db_path);self.addCleanup(self.conn.close)
        logger.disable('wechat_oracle.ingest.writer');self.addCleanup(logger.enable,'wechat_oracle.ingest.writer')
        self.llm=SimpleNamespace(payload=None)
        self.llm.complete_json=lambda **kw:json.dumps(self.llm.payload)
        self.ctx=CommandContext(self.conn,self.llm,'fake','bot','g','class','Alice',20,20,None)
        self.new_request('/kb initial request')

    def message(self,text,*,actor='alice',group='g',wx=None,quote=None,parent=None):
        write_messages(self.conn,[Message(group_id=group,group_name='class' if group=='g' else 'other',
            sender_wxid=actor,sender_display=actor,t=int(datetime.now(s.TZ).timestamp()),type=MsgType.TEXT,
            content_text=text,wx_msg_id=wx or uuid.uuid4().hex,quote_text=quote,reply_to_wx_msg_id=parent,source='backfill')])
        return self.conn.execute('SELECT max(msg_id) FROM messages').fetchone()[0]

    def new_request(self,text,**kwargs):
        self.ctx.trigger_msg_id=self.message(text,**kwargs)
        self.ctx.group_id=kwargs.get('group','g')
        self.ctx.quoted_text=kwargs.get('quote')
        self.ctx.quoted_msg_id=kwargs.get('parent')
        return self.ctx.trigger_msg_id

    def item(self,title='班会',days=2):
        return {'title':title,'kind':'event','people':'全班','location':'301',
                'starts_at':(datetime.now(s.TZ)+timedelta(days=days)).replace(hour=9,minute=0,second=0,microsecond=0).isoformat()}

    def notice(self,title='班会'):
        self.llm.payload={'items':[self.item(title)],'clarification':''}
        c.handle_notice(self.ctx,'通知原文：开班会')
        return self.conn.execute('SELECT max(draft_id) FROM class_notice_drafts').fetchone()[0]

    def confirm(self,draft):
        self.new_request(f'/notice confirm {draft}')
        c.handle_notice(self.ctx,f'confirm {draft}')
        return self.conn.execute('SELECT event_id FROM class_notice_drafts WHERE draft_id=?',(draft,)).fetchone()[0]

    def resource(self,text='课程汇报要求：每组准备十分钟演示和幻灯片。',title='课程汇报要求'):
        actor=s._actor(self.ctx);group=self.ctx.group_id
        self.new_request('/kb add '+text,actor=actor,group=group)
        v.write_resource(self.ctx,'课程A | '+title+' | '+text)
        return self.conn.execute("SELECT max(doc_id) FROM knowledge_documents WHERE kind='resource'").fetchone()[0]

    def drain_index(self,client=None):
        client=client or FakeEmbeddings()
        for _ in range(100):
            if ks.index_batch(self.conn,client,limit=8)==0:break

    def test_source_resolves_original_only_in_same_group(self):
        origin=self.message('真实通知',actor='teacher',wx='original')
        self.message('别的群秘密',group='other',actor='mallory',wx='original')
        command=self.new_request('/notice',quote='真实通知片段',parent='original')
        data=k.source(self.ctx,'提取输入')
        self.assertEqual((data['command_msg_id'],data['origin_msg_id']),(command,origin))
        self.assertEqual(data['origin_text'],'真实通知')
        self.new_request('/notice',quote='只有外部引用',parent='foreign-only')
        self.message('不可跨群解析',group='other',actor='mallory',wx='foreign-only')
        self.assertIsNone(k.source(self.ctx)['origin_msg_id'])

    def test_unconfirmed_notice_is_not_published_or_retrievable(self):
        draft=self.notice('尚未确认通知')
        doc=self.conn.execute('SELECT doc_id FROM knowledge_revisions WHERE revision_id=(SELECT knowledge_revision_id '
                               'FROM class_notice_drafts WHERE draft_id=?)',(draft,)).fetchone()[0]
        with self.assertRaises(ValueError):k.document(self.conn,'g',doc)
        self.assertEqual(ks.search(self.conn,'g','尚未确认')[0],[])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM class_reminders').fetchone()[0],0)

    def test_confirmed_notice_links_event_and_original_snapshot(self):
        origin=self.message('老师真实通知：班会',actor='teacher',wx='teacher-notice')
        self.new_request('/notice',quote='老师真实通知：班会',parent='teacher-notice')
        draft=self.notice();event=self.confirm(draft)
        link=self.conn.execute("SELECT * FROM knowledge_event_links WHERE event_id=? AND role='notice'",(event,)).fetchone()
        _,rev,_=k.document(self.conn,'g',link['doc_id'])
        self.assertEqual(rev['origin_msg_id'],origin)
        self.assertEqual(rev['origin_text'],'老师真实通知：班会')
        self.assertIn('原始消息 #'+str(origin),v.show(self.conn,'g',link['doc_id']))

    def test_revision_preserves_old_until_confirmation_then_cancels_old_reminders(self):
        old=self.confirm(self.notice())
        doc=self.conn.execute("SELECT doc_id FROM knowledge_event_links WHERE event_id=? AND role='notice'",(old,)).fetchone()[0]
        self.new_request('/notice revise '+str(old)+' 修改通知')
        self.llm.payload={'items':[self.item('修改后班会',days=3)],'clarification':''}
        preview=c.handle_notice(self.ctx,f'revise {old} 修改通知')
        self.assertIn('替换旧事项',preview)
        self.assertEqual(k.document(self.conn,'g',doc)[1]['version'],1)
        with self.assertRaises(ValueError):k.document(self.conn,'g',doc,version=2)
        self.assertEqual(self.conn.execute('SELECT cancelled FROM class_events WHERE event_id=?',(old,)).fetchone()[0],0)
        draft=self.conn.execute('SELECT max(draft_id) FROM class_notice_drafts').fetchone()[0]
        new=self.confirm(draft)
        self.assertNotEqual(old,new)
        self.assertEqual(k.document(self.conn,'g',doc)[1]['version'],2)
        self.assertEqual(k.document(self.conn,'g',doc,version=1)[1]['title'],'班会')
        self.assertEqual(self.conn.execute('SELECT cancelled FROM class_events WHERE event_id=?',(old,)).fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM class_reminders WHERE event_id=? AND status='pending'",(old,)).fetchone()[0],0)

    def test_revision_failure_rolls_back_old_cancellation_and_publication(self):
        old=self.confirm(self.notice())
        self.new_request('/notice revise')
        self.llm.payload={'items':[self.item('新安排',days=3)],'clarification':''}
        c.handle_notice(self.ctx,f'revise {old} 新安排')
        draft=self.conn.execute('SELECT max(draft_id) FROM class_notice_drafts').fetchone()[0]
        real=k.activate_notice
        def interrupted(*args):real(*args);raise RuntimeError('stop before commit')
        self.new_request('/notice confirm '+str(draft))
        with patch.object(k,'activate_notice',interrupted),self.assertRaises(RuntimeError):
            c.handle_notice(self.ctx,f'confirm {draft}')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM class_events').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT cancelled FROM class_events WHERE event_id=?',(old,)).fetchone()[0],0)

    def test_resource_versions_and_event_links_are_scoped(self):
        doc=self.resource()
        self.new_request('/schedule');self.llm.payload=self.item()
        s.handle_schedule(self.ctx,'开会');event=self.conn.execute('SELECT max(event_id) FROM class_events').fetchone()[0]
        v.link_resource(self.ctx,event,doc)
        self.new_request('/kb revise')
        v.write_resource(self.ctx,'课程A | 新要求 | 改为十五分钟。',doc_id=doc)
        self.assertEqual(k.document(self.conn,'g',doc)[1]['version'],2)
        self.assertIn('十分钟',k.document(self.conn,'g',doc,version=1)[1]['body'])
        self.assertIn('关联时引用',v.event_sources(self.ctx,event))
        self.assertIn('当前版本',v.event_sources(self.ctx,event))
        self.new_request('other',group='other',actor='mallory')
        with self.assertRaises(ValueError):v.link_resource(self.ctx,event,doc)
        with self.assertRaises(ValueError):k.document(self.conn,'other',doc)

    def test_lexical_search_and_short_chinese_terms_are_public_and_scoped(self):
        self.resource('班费公示与收据。',title='班费')
        self.new_request('other',group='other',actor='mallory')
        self.resource('别群机密班费。',title='秘密班费')
        hits,_=ks.search(self.conn,'g','班费')
        self.assertEqual(len(hits),1)
        self.assertNotIn('秘密',hits[0]['text'])

    def test_semantic_search_filters_group_before_scoring(self):
        self.resource('本群材料')
        self.new_request('other',group='other',actor='mallory');self.resource('别群材料')
        self.drain_index()
        settings.embeddings_enabled=True
        with patch.object(ks,'Embeddings',return_value=FakeEmbeddings()):
            hits,mode=ks.search(self.conn,'g','没有相同关键词的释义')
        self.assertEqual(len(hits),1);self.assertNotIn('别群',hits[0]['text']);self.assertIn('语义',mode)

    def test_old_vectors_and_pending_revision_cannot_override_current_version(self):
        doc=self.resource('旧关键字abc要求')
        self.drain_index()
        self.new_request('/kb revise')
        v.write_resource(self.ctx,'课程A | 更新 | 全新xyz要求',doc_id=doc)
        hits,_=ks.search(self.conn,'g','旧关键字abc')
        self.assertFalse(any('旧关键字abc' in h['text'] for h in hits))
        self.drain_index()
        settings.embeddings_enabled=True
        with patch.object(ks,'Embeddings',return_value=FakeEmbeddings()):
            hits,_=ks.search(self.conn,'g','某个语义问题')
        self.assertEqual(hits[0]['version'],2)

    def test_message_updates_enqueue_new_revision_without_overwriting_old(self):
        mid=self.new_request('原始消息正文')
        while ks.sync_messages(self.conn):pass
        doc=self.conn.execute("SELECT doc_id FROM knowledge_documents WHERE external_key=?",(f'message:{mid}',)).fetchone()[0]
        self.conn.execute('UPDATE messages SET transcript=? WHERE msg_id=?',('后来识别出的OCR文本',mid))
        ks.sync_messages(self.conn)
        self.assertEqual(k.document(self.conn,'g',doc)[1]['version'],2)
        self.assertNotIn('OCR',k.document(self.conn,'g',doc,version=1)[1]['body'])

    def test_model_change_requeues_current_vectors_only(self):
        self.resource();self.drain_index(FakeEmbeddings('first'))
        self.drain_index(FakeEmbeddings('second'))
        models={r[0] for r in self.conn.execute('SELECT model FROM knowledge_chunks WHERE embedding IS NOT NULL')}
        self.assertEqual(models,{'second'})

    def test_interface_failure_falls_back_to_lexical_actual_records(self):
        self.resource('班费使用规则。',title='班费规则');settings.embeddings_enabled=True
        with patch.object(ks,'Embeddings',side_effect=RuntimeError('offline')):
            hits,mode=ks.search(self.conn,'g','班费')
        self.assertTrue(hits);self.assertIn('暂不可用',mode)

    def test_embedding_input_normalization_and_query_instruction(self):
        for vector in ([0,0],[float('nan'),1],[float('inf')],[1e308,1e308]):
            with self.assertRaises(ValueError):ks.normalized(vector)
        settings.embeddings_model='test'
        requests=[]
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[3.0,4.0]}]})
        # One fresh client per request, keeping the real HTTP API code path.
        real=httpx.Client
        with patch.object(ks.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(respond),**kw)):
            vectors=ks.Embeddings().encode(['问题'],query=True)
        self.assertTrue(requests[0]['input'][0].startswith('Instruct:'))
        self.assertAlmostEqual(sum(x*x for x in vectors[0]),1.0,places=6)

    def test_natural_resource_plan_does_not_write_until_author_confirms(self):
        a.propose(self.ctx,{'kind':'resource_add','course':'数学','title':'考试范围','body':'第一至三章。'})
        plan=self.conn.execute('SELECT plan_id FROM class_action_plans').fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT count(*) FROM knowledge_documents WHERE kind='resource'").fetchone()[0],0)
        request=self.ctx.trigger_msg_id
        self.new_request('确认执行 '+plan,quote='机器人方案预览',parent='bot-plan')
        a.confirmation(self.ctx,'确认执行 '+plan)
        rev=self.conn.execute("SELECT r.* FROM knowledge_revisions r JOIN knowledge_documents d ON d.doc_id=r.doc_id WHERE d.kind='resource'").fetchone()
        self.assertEqual(rev['command_msg_id'],request)
        self.assertNotIn('机器人方案',rev['origin_text'])


if __name__=='__main__':unittest.main()
