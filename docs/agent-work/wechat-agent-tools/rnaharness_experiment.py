"""One real local gateway/kernel turn with synthetic data; no WeChat sends/jobs."""
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import httpx
from wechat_oracle import rnaharness as r
from wechat_oracle.config import settings
from wechat_oracle.dispatcher import CommandContext

sys.stdout.reconfigure(encoding='utf-8',errors='replace')
settings.rnaharness_groups=['52170266685@chatroom','58191427313@chatroom']
settings.rnaharness_timeout_seconds=180
conn=sqlite3.connect(':memory:',isolation_level=None);conn.row_factory=sqlite3.Row
conn.execute('CREATE TABLE messages(msg_id INTEGER PRIMARY KEY,group_id TEXT,sender_wxid TEXT)')
conn.execute('INSERT INTO messages VALUES(1,?,?)',('52170266685@chatroom','synthetic-user'))
r.init_rnaharness(conn)
notices=[]
ctx=CommandContext(conn,None,'unused','Occamy大小姐','52170266685@chatroom','synthetic-test','synthetic-user',20,20,None,
    trigger_msg_id=1,bot_wxid='isolated-integration-'+uuid.uuid4().hex,
    progress_sender=lambda text:notices.append(text) or True)
try:
    result=r.chat(ctx,'这是隔离接线验证。仅调用 lab_inventory 读取当前工具就绪状态，简要报告就绪工具数量。不要提交任何计算任务，不要调用 lab_* 计算工具。')
    row=conn.execute('SELECT * FROM rnaharness_requests').fetchone()
    assert row['state']=='completed',result.reply_text
    assert row['ack_sent']==1
    with httpx.Client(trust_env=False,timeout=10) as client:
        response=client.get(r.endpoint()+'/tasks/'+row['task_id']);response.raise_for_status()
        events=response.json()['events']
    tools=[e['tool_name'] for e in events if e['type']=='tool.started']
    assert 'lab_inventory' in tools,tools
    assert all(name=='lab_inventory' for name in tools),tools
    output={'passed':True,'state':row['state'],'ack_count':row['ack_sent'],
            'tool_progress_count':row['progress_sent'],'tools':tools,'reply_chars':len(result.reply_text),
            'weChat_sends':0,'compute_jobs_submitted':0,'isolation':'synthetic-account/session and in-memory database'}
    Path(sys.argv[1]).write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print('EXPERIMENT '+json.dumps(output,ensure_ascii=True),flush=True)
finally:
    conn.close()
