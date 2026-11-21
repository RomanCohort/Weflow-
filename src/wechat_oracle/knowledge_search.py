"""Local Qwen embeddings + SQLite lexical/vector hybrid retrieval and index worker."""
import array
import hashlib
import ipaddress
import math
import re
import threading
import time
from urllib.parse import urlsplit

import httpx
from loguru import logger

from .config import settings
from .db import get_conn, transaction
from .knowledge import message_document

QUERY_INSTRUCTION = 'Instruct: Given a query, retrieve relevant class notices, deadlines and learning materials.\nQuery: '


def normalized(vector):
    if not isinstance(vector,list) or not 1 <= len(vector) <= 8192:
        raise ValueError('invalid embedding dimensions')
    if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in vector):
        raise ValueError('non-finite embedding')
    norm=math.sqrt(sum(float(x)*x for x in vector))
    if norm<=0 or not math.isfinite(norm):
        raise ValueError('zero embedding')
    return array.array('f',(float(x)/norm for x in vector))


class Embeddings:
    def __init__(self):
        self.base=settings.embeddings_endpoint.rstrip('/')
        parsed=urlsplit(self.base)
        if (parsed.scheme!='http' or not ipaddress.ip_address(parsed.hostname).is_loopback
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('embeddings endpoint must be a loopback HTTP API')
        self.model=settings.embeddings_model

    def identity(self):
        if not self.model:
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=settings.embeddings_timeout) as client:
                response=client.get(self.base+'/models');response.raise_for_status()
                models=response.json().get('data',[])
            if len(models)!=1 or not isinstance(models[0].get('id'),str):
                raise ValueError('configure an explicit embedding model')
            self.model=models[0]['id']
        return hashlib.sha256((self.model+'|'+settings.embeddings_revision+'|'+QUERY_INSTRUCTION).encode()).hexdigest()

    def encode(self, texts, *, query=False):
        self.identity()
        if not texts or len(texts)>8 or any(not isinstance(x,str) or len(x)>4000 for x in texts):
            raise ValueError('embedding batch exceeds size limit')
        inputs=[QUERY_INSTRUCTION+x if query else x for x in texts]
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=settings.embeddings_timeout) as client:
            response=client.post(self.base+'/embeddings',json={'model':self.model,'input':inputs})
            response.raise_for_status();data=response.json()['data']
        if sorted(x['index'] for x in data)!=list(range(len(texts))):
            raise ValueError('embedding response indexes mismatch')
        vectors=[normalized(x['embedding']) for x in sorted(data,key=lambda x:x['index'])]
        if len({len(x) for x in vectors})!=1:
            raise ValueError('inconsistent embedding dimensions')
        return vectors


def allowed(group, name=None):
    return not settings.groups or bool({group,name} & set(settings.groups))


def sync_messages(conn, limit=30):
    rows=conn.execute('SELECT j.msg_id,m.* FROM knowledge_message_jobs j LEFT JOIN messages m ON m.msg_id=j.msg_id '
                      'ORDER BY j.msg_id LIMIT ?',(limit,)).fetchall()
    with transaction(conn):
        for row in rows:
            if row['group_id'] is not None and allowed(row['group_id'],row['group_name']):
                message_document(conn,row)
            conn.execute('DELETE FROM knowledge_message_jobs WHERE msg_id=?',(row['msg_id'],))
    return len(rows)


def index_batch(conn, client=None, limit=4):
    client=client or Embeddings()
    identity=client.identity()
    # Requeue active records after model/revision changes; never mix vector spaces.
    conn.execute("INSERT OR IGNORE INTO knowledge_embedding_jobs(revision_id) "
                 "SELECT DISTINCT r.revision_id FROM knowledge_revisions r JOIN knowledge_documents d ON d.current_revision_id=r.revision_id "
                 "JOIN knowledge_chunks c ON c.revision_id=r.revision_id WHERE d.status='active' "
                 "AND (c.embedding IS NULL OR c.model<>?)",(identity,))
    permitted = None
    if settings.groups:
        placeholders=','.join('?' for _ in settings.groups)
        permitted={r[0] for r in conn.execute(
            f'SELECT DISTINCT group_id FROM messages WHERE group_id IN ({placeholders}) OR group_name IN ({placeholders})',
            [*settings.groups,*settings.groups])}
        if not permitted:
            return 0
    scope=' AND d.group_id IN ('+','.join('?' for _ in permitted)+')' if permitted is not None else ''
    rows=conn.execute("SELECT j.revision_id,d.group_id FROM knowledge_embedding_jobs j "
                      "JOIN knowledge_revisions r ON r.revision_id=j.revision_id JOIN knowledge_documents d ON d.doc_id=r.doc_id "
                      "WHERE d.current_revision_id=r.revision_id AND d.status='active' AND j.next_attempt<=? "
                      +scope+" ORDER BY j.revision_id LIMIT 20",[time.time(),*(permitted or [])]).fetchall()
    batch=[]
    for row in rows:
        parts=conn.execute('SELECT chunk_id,text,revision_id FROM knowledge_chunks WHERE revision_id=? '
                           'AND (embedding IS NULL OR model<>?) ORDER BY ordinal LIMIT ?',
                           (row['revision_id'],identity,max(0,limit-len(batch)))).fetchall()
        batch.extend(parts)
        if not parts:
            conn.execute('DELETE FROM knowledge_embedding_jobs WHERE revision_id=?',(row['revision_id'],))
        if len(batch)>=limit:
            break
    if not batch:
        return 0
    revisions={x['revision_id'] for x in batch}
    try:
        vectors=client.encode([x['text'] for x in batch])
        with transaction(conn):
            for chunk,vector in zip(batch,vectors):
                conn.execute('UPDATE knowledge_chunks SET embedding=?,model=?,dimensions=? WHERE chunk_id=?',
                             (vector.tobytes(),identity,len(vector),chunk['chunk_id']))
            for rid in revisions:
                left=conn.execute('SELECT 1 FROM knowledge_chunks WHERE revision_id=? AND (embedding IS NULL OR model<>?)',
                                  (rid,identity)).fetchone()
                if not left:
                    conn.execute('DELETE FROM knowledge_embedding_jobs WHERE revision_id=?',(rid,))
        return len(batch)
    except Exception as exc:
        for rid in revisions:
            conn.execute('UPDATE knowledge_embedding_jobs SET attempts=attempts+1,next_attempt=?,error=? WHERE revision_id=?',
                         (time.time()+60,type(exc).__name__,rid))
        logger.warning('knowledge embedding batch failed: {}',type(exc).__name__)
        return 0


ACTIVE_SQL = """
 FROM knowledge_chunks c JOIN knowledge_revisions r ON r.revision_id=c.revision_id
 JOIN knowledge_documents d ON d.current_revision_id=r.revision_id
 WHERE d.group_id=? AND d.status='active' AND r.published=1
 AND (d.kind<>'message' OR NOT EXISTS (
  SELECT 1 FROM knowledge_revisions nr JOIN knowledge_documents nd ON nd.doc_id=nr.doc_id
  WHERE nd.group_id=d.group_id AND nd.kind='notice' AND nd.status='active' AND nr.published=1
   AND ((nr.origin_msg_id IS NOT NULL AND nr.origin_msg_id=r.origin_msg_id) OR nr.command_msg_id=r.origin_msg_id
        OR (nr.origin_wx_id IS NOT NULL AND nr.origin_wx_id=r.origin_wx_id))
 ))
"""


def lexical_terms(query):
    terms=[]
    for part in re.findall(r'[\u4e00-\u9fff]+|[A-Za-z0-9_.-]+',query):
        terms.extend(part[i:i+3] for i in range(len(part)-2)) if len(part)>4 and re.fullmatch(r'[\u4e00-\u9fff]+',part) else terms.append(part)
    return list(dict.fromkeys(x for x in terms if len(x)>=2))[:24]


def search(conn, group, query, limit=8):
    if not isinstance(query,str) or not query.strip() or len(query)>500:
        raise ValueError('检索问题需要1至500字。')
    limit=max(1,min(int(limit),20))
    terms=lexical_terms(query)
    lexical=[]
    if terms:
        expression=' OR '.join('"'+x.replace('"','""')+'"' for x in terms if len(x)>=3)
        if expression:
            scoped=ACTIVE_SQL.replace('FROM knowledge_chunks c',
                                     'FROM knowledge_fts JOIN knowledge_chunks c ON c.chunk_id=knowledge_fts.rowid')
            ids=conn.execute('SELECT c.chunk_id'+scoped+' AND knowledge_fts MATCH ? '
                             'ORDER BY bm25(knowledge_fts) LIMIT 40',(group,expression)).fetchall()
            lexical=[r[0] for r in ids]
        # Exact short identifiers and two-character Chinese terms remain searchable.
        clause=' OR '.join('instr(lower(c.text),lower(?))>0' for _ in terms)
        lexical.extend(r[0] for r in conn.execute('SELECT c.chunk_id'+ACTIVE_SQL+' AND ('+clause+') LIMIT 40', [group,*terms]))
        lexical=list(dict.fromkeys(lexical))
    semantic=[]
    mode='关键词'
    if settings.embeddings_enabled:
        try:
            client=Embeddings();identity=client.identity();vector=client.encode([query],query=True)[0]
            rows=conn.execute('SELECT c.chunk_id,c.embedding,c.dimensions'+ACTIVE_SQL+
                              ' AND c.model=? AND c.embedding IS NOT NULL',(group,identity))
            scores=[];available=0
            for row in rows:
                if row['dimensions']!=len(vector):
                    continue
                other=array.array('f');other.frombytes(row['embedding'])
                if len(other)!=len(vector):
                    continue
                available+=1
                score=sum(a*b for a,b in zip(vector,other))
                if score>=0.35:
                    scores.append((score,row['chunk_id']))
            semantic=[item[1] for item in sorted(scores,reverse=True)[:40]]
            mode='关键词＋Qwen3语义' if available else '关键词（本群语义索引构建中）'
        except Exception as exc:
            mode='关键词（语义接口暂不可用）'
            logger.warning('knowledge semantic search unavailable: {}',type(exc).__name__)
    rankings={}
    for ranking in (lexical,semantic):
        for rank,cid in enumerate(ranking):rankings[cid]=rankings.get(cid,0)+1/(60+rank)
    hits=[];seen=set()
    for cid,_ in sorted(rankings.items(),key=lambda x:x[1],reverse=True):
        row=conn.execute('SELECT c.text,d.doc_id,r.version,r.title,d.kind'+ACTIVE_SQL+' AND c.chunk_id=?',(group,cid)).fetchone()
        if not row or row['doc_id'] in seen:
            continue
        seen.add(row['doc_id']);hits.append(dict(row))
        if len(hits)>=limit:break
    return hits,mode


class KnowledgeWorker:
    def __init__(self):
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='knowledge-indexer',daemon=True)

    def run(self):
        with get_conn() as conn:
            client=Embeddings() if settings.embeddings_enabled else None
            logger.info('knowledge indexer started; embeddings={}',settings.embeddings_enabled)
            while not self.stop.is_set():
                try:
                    sync_messages(conn)
                    if client:index_batch(conn,client)
                except Exception as exc:
                    logger.warning('knowledge indexer cycle failed: {}',type(exc).__name__)
                self.stop.wait(2)

    def close(self):
        self.stop.set();self.thread.join()
