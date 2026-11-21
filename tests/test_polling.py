"""WeFlow polling still archives messages when SSE emits nothing."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from loguru import logger

from wechat_oracle.config import settings
from wechat_oracle.db import init_db,_connect
from wechat_oracle.ingest.polling import PollingFallback


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        keys=('data_dir','media_dir','agent_output_dir','db_path')
        old={k:getattr(settings,k) for k in keys}
        self.addCleanup(lambda:[setattr(settings,k,v) for k,v in old.items()])
        root=Path(self.temp.name)
        settings.data_dir=root;settings.media_dir=root/'media';settings.agent_output_dir=root/'out';settings.db_path=root/'db'
        init_db();self.conn=_connect(settings.db_path);self.addCleanup(self.conn.close)
        self.poll=PollingFallback({'approved@chatroom':'test'},{'approved@chatroom':{'student':'同学'}})
        self.watermarks=self.poll.initialize(self.conn)
        self.now=int(time.time())
        logger.disable('wechat_oracle.ingest.writer');self.addCleanup(logger.enable,'wechat_oracle.ingest.writer')

    def client(self,timestamp=None):
        def response(request):
            self.assertEqual(request.url.params['talker'],'approved@chatroom')
            return httpx.Response(200,json={'messages':[{'localType':1,'createTime':timestamp or self.now,
                                                       'senderUsername':'student','content':'@机器人 测试'}], 'hasMore':False})
        return httpx.Client(base_url='http://127.0.0.1:5031',transport=httpx.MockTransport(response))

    def test_no_sse_event_still_archives_live_message_once(self):
        with self.client() as client:
            self.assertEqual(self.poll.poll_once(client,self.conn,self.watermarks),1)
            self.assertEqual(self.poll.poll_once(client,self.conn,self.watermarks),0)
        row=self.conn.execute('SELECT * FROM messages').fetchone()
        self.assertEqual((row['group_id'],row['source'],row['sender_display']),('approved@chatroom','live','同学'))

    def test_outage_backlog_is_archived_without_triggering_historical_replies(self):
        self.watermarks['approved@chatroom']=self.now-7200
        with self.client(self.now-3600) as client:self.poll.poll_once(client,self.conn,self.watermarks)
        self.assertEqual(self.conn.execute('SELECT source FROM messages').fetchone()[0],'backfill')

    def test_watermark_survives_restart(self):
        with self.client() as client:self.poll.poll_once(client,self.conn,self.watermarks)
        restarted=PollingFallback(self.poll.names,self.poll.members)
        self.assertEqual(restarted.initialize(self.conn)['approved@chatroom'],self.now)

    def test_transport_failure_does_not_advance_watermark(self):
        before=dict(self.watermarks)
        def fail(request):raise httpx.ConnectError('synthetic offline')
        with httpx.Client(base_url='http://127.0.0.1:5031',transport=httpx.MockTransport(fail)) as client:
            self.assertEqual(self.poll.poll_once(client,self.conn,self.watermarks),0)
        self.assertEqual(self.watermarks,before)

    def test_auto_discovery_adds_new_groups_and_updates_renames(self):
        state={
            'sessions':[
                {'username':'new@chatroom','displayName':'旧群名','sessionType':'group'},
                {'username':'dm-user','displayName':'私聊','sessionType':'single'},
            ]
        }

        def response(request):
            path=request.url.path
            if path=='/api/v1/sessions':
                return httpx.Response(200,json={'sessions':state['sessions']})
            if path=='/api/v1/group-members':
                return httpx.Response(200,json={'members':[{'username':'member','displayName':'成员'}]})
            if path=='/api/v1/messages':
                return httpx.Response(200,json={'messages':[],'hasMore':False})
            return httpx.Response(404)

        poll=PollingFallback({}, {}, watermarks={}, auto_discover=True)
        with patch.object(settings,'groups',[]), \
             patch.object(settings,'direct_messages_enabled',True), \
             httpx.Client(base_url='http://127.0.0.1:5031',transport=httpx.MockTransport(response)) as client:
            self.assertEqual(poll.refresh_sessions(client),(2,0))
            self.assertEqual(poll.names['new@chatroom'],'旧群名')
            self.assertIn('dm-user',poll.names)
            state['sessions'][0]['displayName']='Occamy会梦见电子吴姨吗'
            self.assertEqual(poll.refresh_sessions(client),(0,1))
            self.assertEqual(
                poll.names['new@chatroom'],
                'Occamy会梦见电子吴姨吗',
            )


if __name__=='__main__':unittest.main()
