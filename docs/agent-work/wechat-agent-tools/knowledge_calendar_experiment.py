"""Real dev-host Qwen/Occamy verification with temporary synthetic data only."""
import json
import tempfile
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path

from wechat_oracle import knowledge as k,knowledge_search as search,knowledge_views as views
from wechat_oracle import class_actions as actions,classroom
from wechat_oracle.agent.backends.native import NativeBackend
from wechat_oracle.calendar_intent import calendar_entry
from wechat_oracle.config import settings
from wechat_oracle.db import init_db,_connect
from wechat_oracle.dispatcher import CommandContext,_build_llm_client
from wechat_oracle.ingest.writer import write_messages
from wechat_oracle.models import Message,MsgType
from wechat_oracle.schedule import TZ


def message(conn,group,actor,text):
    write_messages(conn,[Message(group_id=group,group_name=group,sender_wxid=actor,sender_display=actor,
                                 t=int(datetime.now(TZ).timestamp()),type=MsgType.TEXT,content_text=text,source='backfill')])
    return conn.execute('SELECT max(msg_id) FROM messages').fetchone()[0]


def main():
    import sys
    output=Path(sys.argv[1])
    llm=_build_llm_client()
    results=[]
    with tempfile.TemporaryDirectory(prefix='knowledge-calendar-experiment-') as directory:
        root=Path(directory)
        settings.data_dir=root;settings.media_dir=root/'media';settings.agent_output_dir=root/'outputs'
        settings.db_path=root/'test.db';settings.agent_personas_dir=root/'personas';settings.groups=[]
        settings.embeddings_enabled=True
        (root/'class_officers.json').write_text(json.dumps({'groups':{'experiment':['officer'],'other':['other-officer']}}))
        init_db();conn=_connect(settings.db_path)
        try:
            ctx=CommandContext(conn,llm,settings.llm_model,settings.bot_name,'experiment','experiment','officer',20,20,None)
            resources=[('experiment','officer','课程A | 课堂展示安排 | 各小组需准备十分钟课堂演示，制作幻灯片并说明分工。'),
                       ('experiment','officer','体育 | 体育训练 | 在体育场进行跑步和拉伸训练。'),
                       ('other','other-officer','秘密 | PPT汇报规范 | OTHER_GROUP_SECRET 小组汇报PPT规范。')]
            for group,actor,body in resources:
                mid=message(conn,group,actor,'保存资料：'+body)
                local=replace(ctx,group_id=group,group_name=group,requester=actor,trigger_msg_id=mid)
                views.write_resource(local,body)
            settings.embeddings_enabled=False
            lexical,_=search.search(conn,'experiment','小组汇报的PPT规范')
            assert not lexical
            settings.embeddings_enabled=True
            client=search.Embeddings()
            for _ in range(20):
                if search.index_batch(conn,client,limit=4)==0:break
            hits,mode=search.search(conn,'experiment','小组汇报的PPT规范')
            assert any('课堂展示安排' in h['title'] for h in hits)
            assert all('OTHER_GROUP_SECRET' not in h['text'] for h in hits)
            dimensions={r[0] for r in conn.execute('SELECT dimensions FROM knowledge_chunks WHERE embedding IS NOT NULL')}
            assert dimensions=={1024}
            results.append({'case':'real_qwen_semantic_and_group_isolation','passed':True,
                            'dimensions':sorted(dimensions),'mode':mode,'hit_titles':[h['title'] for h in hits]})
            results[-1]['lexical_baseline_hits']=len(lexical)
            text='明天早上7点准时提醒，在体育场集合'
            mid=message(conn,'experiment','officer',text);ctx=replace(ctx,trigger_msg_id=mid)
            preview=calendar_entry(ctx,text)
            plan=conn.execute('SELECT * FROM class_action_plans ORDER BY created_at DESC LIMIT 1').fetchone()
            assert plan and '尚未执行' in preview
            payload=json.loads(plan['payload']);stamp=datetime.fromisoformat(payload['starts_at'])
            assert stamp.date()==(datetime.now(TZ)+timedelta(days=1)).date() and (stamp.hour,stamp.minute)==(7,0)
            assert '体育场' in payload['location'] and payload['remind_at_start'] is True
            assert conn.execute('SELECT count(*) FROM class_events').fetchone()[0]==0
            confirm='确认执行 '+plan['plan_id'];confirm_id=message(conn,'experiment','officer',confirm)
            receipt=actions.confirmation(replace(ctx,trigger_msg_id=confirm_id),confirm)
            row=conn.execute("SELECT * FROM class_reminders WHERE label='当天'").fetchone()
            assert datetime.fromtimestamp(row['due_at'],TZ).hour==7 and '执行回执' in receipt
            board=classroom.dashboard(ctx,week=True)
            assert '来源' in board and text in board
            results.append({'case':'real_occamy_exact_0700_calendar','passed':True,'location':payload['location'],
                            'time':stamp.isoformat(),'events_after_confirmation':1,'source_on_dashboard':True})
            for row in results:print('EXPERIMENT '+json.dumps(row,ensure_ascii=True),flush=True)
            output.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            conn.close()


if __name__=='__main__':main()
