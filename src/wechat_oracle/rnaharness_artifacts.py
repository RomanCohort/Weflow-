"""Deliver proven RhoFold PDBs to the bound source group, without replay."""
import math
import re
import time
from loguru import logger

from .agent.tools_files import atomic_write_bytes, group_output_root
from .config import settings
from .db import transaction

SCHEMA = '''
CREATE TABLE IF NOT EXISTS rnaharness_artifacts (
 request_id INTEGER NOT NULL, worker TEXT NOT NULL, job_id TEXT NOT NULL,
 name TEXT NOT NULL, state TEXT NOT NULL, output_name TEXT, updated_at REAL NOT NULL,
 PRIMARY KEY(request_id,worker,job_id,name)
);
'''


def validate(event):
    if (event.get('worker') != 'rhofold'
            or not isinstance(event.get('job_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', event['job_id'])
            or not isinstance(event.get('name'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}\.pdb', event['name'])):
        raise ValueError('unsupported research artifact')


def pdb_bytes(client, event):
    # No model-supplied URLs, filesystem paths, or worker ports.
    validate(event)
    base = 'http://127.0.0.1:8011'
    response = client.get(base + '/api/result/' + event['job_id'])
    response.raise_for_status()
    result = response.json()
    if result.get('status') != 'done' or event['name'] not in (result.get('files') or []):
        raise ValueError('worker did not confirm a completed PDB')
    cap = min(settings.agent_output_max_bytes, 1024 * 1024)
    chunks = []; size = 0
    with client.stream('GET', base + '/api/file/' + event['job_id'] + '/' + event['name']) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > cap:
                raise ValueError('PDB exceeds output limit')
            chunks.append(chunk)
    data = b''.join(chunks)
    atoms = 0
    for line in data.decode('ascii').splitlines():
        if line.startswith(('ATOM  ', 'HETATM')):
            if len(line) < 54 or not all(math.isfinite(float(line[a:b]))
                                       for a, b in ((30, 38), (38, 46), (46, 54))):
                raise ValueError('invalid PDB coordinates')
            atoms += 1
    if not atoms:
        raise ValueError('PDB has no atom coordinates')
    return data


def deliver(ctx, row, events, client):
    pending = [e for e in events if e.get('type') == 'artifact.ready']
    if not pending:
        return ''
    source = ctx.conn.execute('SELECT 1 FROM rnaharness_requests r JOIN messages m '
                              'ON m.msg_id=r.source_msg_id AND m.group_id=r.group_id '
                              'WHERE r.request_id=? AND r.group_id=? AND r.task_id=?',
                              (row['request_id'], ctx.group_id, row['task_id'])).fetchone()
    if not source:
        raise ValueError('artifact source group mismatch')
    notes = []; seen = set()
    for event in pending[:4]:
        try:
            validate(event)
            if event.get('task_id') != row['task_id']:
                raise ValueError('artifact task mismatch')
        except (ValueError, TypeError):
            notes.append('PDB附件信息无效，未下载或发送。')
            continue
        key = (row['request_id'], event['worker'], event['job_id'], event['name'])
        if key in seen:
            continue
        seen.add(key)
        old = ctx.conn.execute('SELECT state FROM rnaharness_artifacts WHERE '
                               'request_id=? AND worker=? AND job_id=? AND name=?', key).fetchone()
        if old:
            notes.append('PDB附件已确认发送。' if old['state'] == 'sent' else
                         'PDB附件先前处理未成功或状态不确定，未自动重试。')
            continue
        with transaction(ctx.conn):
            ctx.conn.execute('INSERT INTO rnaharness_artifacts VALUES(?,?,?,?,?,?,?)',
                             (*key, 'preparing', None, time.time()))
        state = 'failed'; output_name = None; saved = False
        try:
            data = pdb_bytes(client, event)
            root = group_output_root(ctx.group_id)
            root.mkdir(parents=True, exist_ok=True)
            output_name = f"rnaharness-{row['request_id']}-{event['job_id']}-{event['name']}"
            path = root / output_name
            atomic_write_bytes(path, data)
            saved = True
            sender = getattr(ctx, 'attachment_sender', None)
            if not sender:
                raise ValueError('attachment sender unavailable')
            # Persist before the GUI side effect. A crash after send is uncertain.
            state = 'sending'
            with transaction(ctx.conn):
                ctx.conn.execute('UPDATE rnaharness_artifacts SET state=?,output_name=?,updated_at=? '
                                 'WHERE request_id=? AND worker=? AND job_id=? AND name=?',
                                 (state, output_name, time.time(), *key))
            if sender(ctx.group_name, path, None) is False:
                raise ValueError('attachment send unconfirmed')
            state = 'sent'
            notes.append('PDB附件已确认发送：' + output_name + '（模型预测结构）。')
        except Exception as exc:
            logger.warning('RNAHarness artifact delivery failed: request={} error={}',
                           row['request_id'], type(exc).__name__)
            state = 'uncertain' if state == 'sending' else 'failed'
            notes.append('PDB已保存，但群内发送未确认；未自动重发。' if saved else
                         'PDB下载或校验失败，未作为附件发送。')
        with transaction(ctx.conn):
            ctx.conn.execute('UPDATE rnaharness_artifacts SET state=?,output_name=?,updated_at=? '
                             'WHERE request_id=? AND worker=? AND job_id=? AND name=?',
                             (state, output_name, time.time(), *key))
    return '\n\n' + '\n'.join(notes)
