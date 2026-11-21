import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from wechat_oracle import rnaharness as r
from wechat_oracle import rnaharness_artifacts as a
from wechat_oracle.config import settings

PDB = b'ATOM      1  P     A A   1      10.000  11.000  12.000  1.00 20.00           P\nEND\n'


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,group_id TEXT,sender_wxid TEXT)')
        self.conn.execute("INSERT INTO messages VALUES(1,'g','alice')")
        r.init_rnaharness(self.conn)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        for key, value in {'agent_output_dir': Path(temp.name), 'rnaharness_groups': ['g'],
                           'rnaharness_endpoint': 'http://127.0.0.1:8787'}.items():
            p = patch.object(settings, key, value); p.start(); self.addCleanup(p.stop)
        self.sent = []; self.urls = []; self.data = PDB; self.status = 'done'; self.files = ['model.pdb']
        self.ctx = SimpleNamespace(conn=self.conn, group_id='g', group_name='test', bot_wxid='bot',
                                   trigger_msg_id=1, requester='Alice', progress_sender=None,
                                   attachment_sender=lambda group, path, caption: self.sent.append((group, path.read_bytes())))
        self.event = dict(type='artifact.ready', task_id='task-1', worker='rhofold', job_id='job-1', name='model.pdb')
        self.events = [self.event, dict(type='answer.completed', task_id='task-1', text='预测完成。')]
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)

    def handle(self, request):
        self.urls.append(str(request.url))
        if request.method == 'POST':
            return httpx.Response(202, json={'task_id': 'task-1'})
        if request.url.port == 8787:
            return httpx.Response(200, json={'events': self.events})
        if '/api/result/' in request.url.path:
            return httpx.Response(200, json={'status': self.status, 'files': self.files})
        return httpx.Response(200, content=self.data)

    def test_success_is_saved_sent_and_never_replayed(self):
        result = r.chat(self.ctx, '预测RNA，输出pdb', client=self.client)
        self.assertIn('PDB附件已确认发送', result.reply_text)
        self.assertEqual(self.sent, [('test', PDB)])
        self.assertEqual(self.conn.execute('SELECT state FROM rnaharness_artifacts').fetchone()[0], 'sent')
        count = len(self.urls)
        r.chat(self.ctx, '预测RNA，输出pdb', client=self.client)
        self.assertEqual(len(self.urls), count)
        self.assertEqual(len(self.sent), 1)

    def test_invalid_artifacts_do_not_download(self):
        for field, value in [('name', '../secret.pdb'), ('job_id', '../other'), ('worker', 'arbitrary')]:
            event = {**self.event, field: value}
            with self.assertRaises(ValueError):
                a.pdb_bytes(self.client, event)
        self.assertEqual(self.urls, [])

    def test_requires_done_file_membership_and_real_coordinates(self):
        for status, files, data in [('running', self.files, PDB), ('done', [], PDB),
                                    ('done', self.files, b'<html>not a PDB</html>')]:
            self.status, self.files, self.data = status, files, data
            with self.assertRaises(ValueError):
                a.pdb_bytes(self.client, self.event)

    def test_kernel_failure_never_sends_partial_artifact(self):
        self.events.append(dict(type='task.failed', task_id='task-1', message='tool loop exceeded max_turns'))
        result = r.chat(self.ctx, '输出pdb', client=self.client)
        self.assertIn('TOOL_LOOP_EXCEEDED', result.reply_text)
        self.assertEqual(self.sent, [])
        self.assertFalse(any(':8011/' in url for url in self.urls))

    def test_send_uncertainty_is_persisted_without_retry(self):
        self.ctx.attachment_sender = lambda *args: False
        result = r.chat(self.ctx, '输出pdb', client=self.client)
        self.assertIn('群内发送未确认', result.reply_text)
        state = self.conn.execute('SELECT state FROM rnaharness_artifacts').fetchone()[0]
        self.assertEqual(state, 'uncertain')
        row = self.conn.execute('SELECT * FROM rnaharness_requests').fetchone()
        count = len(self.urls)
        a.deliver(self.ctx, row, [self.event], self.client)
        self.assertEqual(len(self.urls), count)

    def test_cross_group_or_task_is_rejected(self):
        row, _ = r.prepare(self.ctx, '输出pdb')
        r.update(self.conn, row['request_id'], 'running', task='task-1')
        row = self.conn.execute('SELECT * FROM rnaharness_requests').fetchone()
        self.ctx.group_id = 'other'
        with self.assertRaisesRegex(ValueError, 'group'):
            a.deliver(self.ctx, row, [self.event], self.client)
        self.ctx.group_id = 'g'
        note = a.deliver(self.ctx, row, [{**self.event, 'task_id': 'other'}], self.client)
        self.assertIn('信息无效', note)
        self.assertEqual(self.urls, [])

    def test_size_cap_before_send(self):
        with patch.object(settings, 'agent_output_max_bytes', 20):
            with self.assertRaisesRegex(ValueError, 'limit'):
                a.pdb_bytes(self.client, self.event)
        self.assertEqual(self.sent, [])
