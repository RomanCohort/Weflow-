"""Canonical group knowledge, immutable revisions, provenance, and event links.

Embeddings are derived indexes. Published revision pointers and event state are
authoritative. A notice revision replaces exactly one owned event on confirmation;
other items extracted from the same original announcement remain independent.
"""
import hashlib
import json
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_documents (
 doc_id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, kind TEXT NOT NULL,
 owner TEXT NOT NULL, external_key TEXT NOT NULL, current_revision_id INTEGER,
 status TEXT NOT NULL DEFAULT 'draft', created_at REAL NOT NULL,
 UNIQUE(group_id,external_key)
);
CREATE TABLE IF NOT EXISTS knowledge_revisions (
 revision_id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES knowledge_documents(doc_id),
 version INTEGER NOT NULL, title TEXT NOT NULL, course TEXT NOT NULL DEFAULT '',
 body TEXT NOT NULL, digest TEXT NOT NULL, command_msg_id INTEGER,
 origin_msg_id INTEGER, origin_wx_id TEXT, quote_text TEXT NOT NULL DEFAULT '', origin_text TEXT NOT NULL DEFAULT '',
 previous_revision_id INTEGER, created_at REAL NOT NULL, published INTEGER NOT NULL DEFAULT 0, UNIQUE(doc_id,version)
);
CREATE TABLE IF NOT EXISTS knowledge_event_links (
 event_id INTEGER NOT NULL REFERENCES class_events(event_id),
 doc_id INTEGER NOT NULL REFERENCES knowledge_documents(doc_id),
 revision_id INTEGER NOT NULL REFERENCES knowledge_revisions(revision_id),
 role TEXT NOT NULL, PRIMARY KEY(event_id,doc_id,role)
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
 chunk_id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES knowledge_revisions(revision_id),
 ordinal INTEGER NOT NULL, text TEXT NOT NULL, embedding BLOB, model TEXT, dimensions INTEGER,
 UNIQUE(revision_id,ordinal)
);
CREATE TABLE IF NOT EXISTS knowledge_embedding_jobs (
 revision_id INTEGER PRIMARY KEY REFERENCES knowledge_revisions(revision_id),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_message_jobs (msg_id INTEGER PRIMARY KEY);
CREATE INDEX IF NOT EXISTS knowledge_group ON knowledge_documents(group_id,status);
CREATE INDEX IF NOT EXISTS knowledge_origin ON knowledge_revisions(origin_msg_id);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(text,tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS knowledge_message_insert AFTER INSERT ON messages BEGIN
 INSERT OR IGNORE INTO knowledge_message_jobs(msg_id) VALUES(NEW.msg_id);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_message_update AFTER UPDATE OF content_text,transcript,quote_text ON messages BEGIN
 INSERT OR IGNORE INTO knowledge_message_jobs(msg_id) VALUES(NEW.msg_id);
END;
"""


def ready(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_documents'").fetchone())


def init_knowledge(conn):
    first = not ready(conn)
    conn.executescript(SCHEMA)
    if first:
        conn.execute('INSERT OR IGNORE INTO knowledge_message_jobs SELECT msg_id FROM messages')


def val(row, key, default=None):
    return row[key] if row is not None and key in row.keys() and row[key] is not None else default


def source(ctx, text='', request_msg_id=None):
    mid = request_msg_id if request_msg_id is not None else ctx.trigger_msg_id
    row = ctx.conn.execute('SELECT * FROM messages WHERE msg_id=? AND group_id=?', (mid, ctx.group_id)).fetchone()
    if row is None:
        raise ValueError('找不到本群的来源消息，未保存资料。')
    same = mid == ctx.trigger_msg_id
    quote = val(row, 'quote_text', '') or ((getattr(ctx, 'quoted_text', '') or '') if same else '')
    wxid = val(row, 'reply_to_wx_msg_id') or (getattr(ctx, 'quoted_msg_id', None) if same else None)
    origin = None
    if wxid and 'wx_msg_id' in {r['name'] for r in ctx.conn.execute('PRAGMA table_info(messages)')}:
        origin = ctx.conn.execute('SELECT * FROM messages WHERE group_id=? AND wx_msg_id=? ORDER BY msg_id LIMIT 1',
                                  (ctx.group_id, wxid)).fetchone()
    # Never resolve a quoted ID across group boundaries or invent the parent ID.
    return {'command_msg_id': mid, 'origin_msg_id': val(origin, 'msg_id', None if quote or wxid else mid),
            'origin_wx_id': wxid or val(row, 'wx_msg_id'), 'quote_text': quote,
            'origin_text': val(origin,'content_text','') or quote or val(row,'content_text',''),
            'body': text or '\n'.join(x for x in (val(origin, 'content_text', ''), quote,
                                                  val(row, 'content_text', '')) if x)}


def new_document(conn, group, kind, owner, key):
    conn.execute('INSERT OR IGNORE INTO knowledge_documents(group_id,kind,owner,external_key,created_at) VALUES(?,?,?,?,?)',
                 (group, kind, owner, key, time.time()))
    return conn.execute('SELECT * FROM knowledge_documents WHERE group_id=? AND external_key=?', (group, key)).fetchone()


def revision(conn, document, title, body, provenance, *, course='', publish=False):
    digest = hashlib.sha256((title+'\n'+course+'\n'+body).encode()).hexdigest()
    last = conn.execute('SELECT * FROM knowledge_revisions WHERE doc_id=? ORDER BY version DESC LIMIT 1',
                        (document['doc_id'],)).fetchone()
    if last and last['digest'] == digest and last['command_msg_id'] == provenance.get('command_msg_id'):
        return last['revision_id']
    cur = conn.execute('INSERT INTO knowledge_revisions(doc_id,version,title,course,body,digest,command_msg_id,'
                       'origin_msg_id,origin_wx_id,quote_text,origin_text,previous_revision_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (document['doc_id'], last['version']+1 if last else 1, title, course, body, digest,
                        provenance.get('command_msg_id'), provenance.get('origin_msg_id'),
                        provenance.get('origin_wx_id'), provenance.get('quote_text',''),
                        provenance.get('origin_text',''),
                        last['revision_id'] if last else None, time.time()))
    rid = cur.lastrowid
    # Long archive messages remain intact in messages; their first 16k are indexed.
    text = title+'\n'+course+'\n'+body[:16000]
    for ordinal, start in enumerate(range(0,len(text),1080)):
        chunk = text[start:start+1200]
        chunk_id = conn.execute('INSERT INTO knowledge_chunks(revision_id,ordinal,text) VALUES(?,?,?)',
                                (rid,ordinal,chunk)).lastrowid
        conn.execute('INSERT INTO knowledge_fts(rowid,text) VALUES(?,?)',(chunk_id,chunk))
    conn.execute('INSERT OR IGNORE INTO knowledge_embedding_jobs(revision_id) VALUES(?)',(rid,))
    if publish:
        conn.execute('UPDATE knowledge_revisions SET published=1 WHERE revision_id=?',(rid,))
        conn.execute("UPDATE knowledge_documents SET current_revision_id=?,status='active' WHERE doc_id=?",
                     (rid, document['doc_id']))
    return rid


def message_document(conn, row):
    body = '\n'.join(x for x in (val(row,'content_text',''), val(row,'transcript',''), val(row,'quote_text','')) if x)
    if not body.strip():
        return None
    doc = new_document(conn,row['group_id'],'message',val(row,'sender_wxid','unknown'), f"message:{row['msg_id']}")
    revision(conn,doc,body.splitlines()[0][:120],body,
             {'command_msg_id':row['msg_id'],'origin_msg_id':row['msg_id'],'origin_wx_id':val(row,'wx_msg_id'),
              'origin_text':body},publish=True)
    return conn.execute('SELECT * FROM knowledge_documents WHERE doc_id=?',(doc['doc_id'],)).fetchone()


def link_request(ctx, event, actor, request_msg_id=None):
    if not ready(ctx.conn):
        return
    mid = request_msg_id if request_msg_id is not None else ctx.trigger_msg_id
    row = ctx.conn.execute('SELECT * FROM messages WHERE msg_id=? AND group_id=?',(mid,ctx.group_id)).fetchone()
    if row is None:
        raise ValueError('事项来源消息不属于本群。')
    doc = message_document(ctx.conn,row)
    if doc:
        ctx.conn.execute('INSERT OR IGNORE INTO knowledge_event_links VALUES(?,?,?,?)',
                         (event['event_id'],doc['doc_id'],doc['current_revision_id'],'request'))


def owned_event(ctx, item_id):
    from .schedule import _actor
    row = ctx.conn.execute('SELECT * FROM class_events WHERE event_id=? AND group_id=? AND creator=? AND cancelled=0',
                           (item_id,ctx.group_id,_actor(ctx))).fetchone()
    if not row:
        raise ValueError('只能修订你在本群创建的有效事项。')
    return row


def notice_revisions(ctx, body, items, replacement=None):
    if not ready(ctx.conn):
        return [None for _ in items]
    from .schedule import _actor
    provenance = source(ctx,body)
    ids=[]
    for index,item in enumerate(items):
        if replacement:
            link = ctx.conn.execute("SELECT d.* FROM knowledge_event_links l JOIN knowledge_documents d ON d.doc_id=l.doc_id "
                                    "WHERE l.event_id=? AND l.role='notice' AND d.group_id=?", (replacement['event_id'],ctx.group_id)).fetchone()
        else:
            link = None
        doc = link or new_document(ctx.conn,ctx.group_id,'notice',_actor(ctx),f'notice:{ctx.trigger_msg_id}:{index}')
        if link:
            pending = ctx.conn.execute("SELECT 1 FROM class_notice_drafts WHERE replaces_event_id=? AND status='pending' AND expires_at>?",
                                       (replacement['event_id'],time.time())).fetchone()
            if pending:
                raise ValueError('该事项已有待确认修订，请先确认或放弃旧草稿。')
        ids.append(revision(ctx.conn,doc,item['title'],body,provenance))
    return ids


def validate_replacement(ctx, draft):
    if val(draft,'replaces_event_id'):
        owned_event(ctx,draft['replaces_event_id'])


def activate_notice(ctx, draft, event):
    rid = val(draft,'knowledge_revision_id')
    if not rid:
        return
    validate_replacement(ctx,draft)
    rev = ctx.conn.execute('SELECT r.* FROM knowledge_revisions r JOIN knowledge_documents d ON d.doc_id=r.doc_id '
                           'WHERE r.revision_id=? AND d.group_id=?',(rid,ctx.group_id)).fetchone()
    if not rev:
        raise ValueError('通知来源版本不属于本群。')
    if val(draft,'replaces_event_id'):
        old=draft['replaces_event_id']
        ctx.conn.execute('UPDATE class_events SET cancelled=1 WHERE event_id=?',(old,))
        ctx.conn.execute("UPDATE class_reminders SET status='cancelled' WHERE event_id=? AND status='pending'",(old,))
    ctx.conn.execute("UPDATE knowledge_documents SET current_revision_id=?,status='active' WHERE doc_id=?",(rid,rev['doc_id']))
    ctx.conn.execute('UPDATE knowledge_revisions SET published=1 WHERE revision_id=?',(rid,))
    ctx.conn.execute('INSERT OR IGNORE INTO knowledge_event_links VALUES(?,?,?,?)',(event['event_id'],rev['doc_id'],rid,'notice'))


def document(conn, group, doc_id, *, version=None):
    if not isinstance(doc_id,int) or isinstance(doc_id,bool) or doc_id<1:
        raise ValueError('资料编号必须为正整数。')
    doc=conn.execute('SELECT * FROM knowledge_documents WHERE doc_id=? AND group_id=?',(doc_id,group)).fetchone()
    if not doc or doc['status']!='active':
        raise ValueError('本群没有这条已发布资料。')
    if version is None:
        rev=conn.execute('SELECT * FROM knowledge_revisions WHERE revision_id=?',(doc['current_revision_id'],)).fetchone()
    else:
        rev=conn.execute('SELECT * FROM knowledge_revisions WHERE doc_id=? AND version=? AND published=1',(doc_id,version)).fetchone()
    if not rev:
        raise ValueError('资料版本不存在。')
    links=conn.execute('SELECT e.event_id,e.title,e.kind,e.starts_at,e.cancelled,l.role,lr.version AS linked_version FROM knowledge_event_links l '
                       'JOIN knowledge_revisions lr ON lr.revision_id=l.revision_id '
                       'JOIN class_events e ON e.event_id=l.event_id WHERE l.doc_id=? AND e.group_id=? ORDER BY e.event_id DESC LIMIT 20',
                       (doc_id,group)).fetchall()
    return doc,rev,links
