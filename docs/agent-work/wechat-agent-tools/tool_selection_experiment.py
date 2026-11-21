"""Run explicitly on the dev host: real configured LLM, temporary synthetic DB.

Does not instantiate wx4py, send group messages, modify production officers/data,
or submit image jobs. Output reports selected tools and observed stored effects.
"""
import argparse
import json
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from wechat_oracle.agent.backends.native import NativeBackend
from wechat_oracle.agent.tools import ToolError
from wechat_oracle.class_actions import confirmation
from wechat_oracle.config import settings
from wechat_oracle.db import _connect, init_db, transaction
from wechat_oracle.dispatcher import CommandContext, _build_llm_client
from wechat_oracle.ingest.writer import write_messages
from wechat_oracle.models import Message, MsgType
from wechat_oracle.schedule import TZ, persist_event


class ObservedLLM:
    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete_with_tools(self, **kwargs):
        response = self.inner.complete_with_tools(**kwargs)
        self.calls.extend(call.name for call in response.tool_calls)
        return response


def message(conn, group, actor, text):
    write_messages(conn, [Message(group_id=group, group_name='隔离实验群', sender_wxid=actor,
                                  sender_display=actor, t=int(datetime.now(TZ).timestamp()),
                                  type=MsgType.TEXT, content_text=text, source='backfill')])
    return conn.execute('SELECT max(msg_id) FROM messages').fetchone()[0]


def run_case(name, root, real_llm):
    settings.data_dir = root
    settings.media_dir = root/'media'
    settings.agent_output_dir = root/'outputs'
    settings.agent_personas_dir = root/'personas'
    settings.db_path = root/'experiment.db'
    settings.agent_max_steps = 5
    group, officer, ordinary = 'experiment@chatroom', 'experiment-officer', 'experiment-member'
    init_db(settings.db_path)
    (root/'class_officers.json').write_text(json.dumps({'groups':{group:[officer]}}), encoding='utf-8')
    conn = _connect(settings.db_path)
    try:
        llm = ObservedLLM(real_llm)
        now = datetime.now(TZ)
        day = (now+timedelta(days=2)).strftime('%Y-%m-%d')
        requests = {
            'public_query': '请查询数据库里今天已登记的活动和截止事项。',
            'event_proposal': f'请帮我登记{day}上午9点全班在301开班会，并提前提醒。先给出方案，不要直接执行。',
            'todo_proposal': f'请记下{day}下午5点前全班向教务办公室交实验报告，提前提醒。先说明执行方案。',
            'unauthorized': f'请替全班登记{day}上午9点在301的班会并自动提醒。',
        }
        actor = ordinary if name in {'public_query', 'unauthorized'} else officer
        seed_id = message(conn, group, officer, '合成数据：今天有隔离实验班会。')
        ctx = CommandContext(conn, llm, settings.llm_model, settings.bot_name, group, '隔离实验群',
                             officer, 20, 20, None, trigger_msg_id=seed_id,
                             attachment_sender=lambda *x: (_ for _ in ()).throw(AssertionError('no UI sends')))
        seed_time = now.replace(hour=14, minute=0, second=0, microsecond=0)
        with transaction(conn):
            persist_event(ctx, {'title':'隔离实验班会','people':'实验成员','location':'TEST301'},
                          seed_time, [], officer, now)
        text = requests[name]
        msg_id = message(conn, group, actor, text)
        ctx = replace(ctx, requester=actor, trigger_msg_id=msg_id)
        with patch('wechat_oracle.agent.krea2.GenerateGroupImageTool.call',
                   side_effect=ToolError('image jobs are disabled in experiments')):
            outcome = NativeBackend().chat(ctx=ctx, user_question=text, trigger_kind='mention', reflection_enabled=False)
        plans = conn.execute('SELECT * FROM class_action_plans ORDER BY created_at').fetchall()
        events = conn.execute('SELECT count(*) FROM class_events').fetchone()[0]
        result = {'case':name, 'selected_tools':llm.calls, 'plan_count':len(plans),
                  'new_events_before_confirmation':events-1,
                  'reminders_before_confirmation':conn.execute('SELECT count(*) FROM class_reminders').fetchone()[0]}
        result['active_conversation_followups'] = conn.execute(
            "SELECT count(*) FROM agent_proactive_outbox WHERE status IN ('planned','pending','running')"
        ).fetchone()[0]
        if name == 'public_query':
            passed = 'query_classroom' in llm.calls and '隔离实验班会' in (outcome.reply_text or '') and not plans
        elif name == 'unauthorized':
            passed = (not plans and events == 1 and 'propose_class_action' not in llm.calls
                      and result['active_conversation_followups']==0)
        else:
            expected = 'event' if name == 'event_proposal' else 'todo'
            payload = json.loads(plans[0]['payload']) if plans else {}
            expected_hour = 9 if expected == 'event' else 17
            expected_location = '301' if expected == 'event' else '教务办公室'
            passed = (len(plans)==1 and plans[0]['kind']==expected and events==1
                      and datetime.fromisoformat(payload['starts_at']).date()==(now+timedelta(days=2)).date()
                      and datetime.fromisoformat(payload['starts_at']).hour==expected_hour
                      and expected_location in payload['location'])
            if passed:
                confirm_text = '确认执行 '+plans[0]['plan_id']
                confirm_id = message(conn, group, actor, confirm_text)
                before_calls = len(llm.calls)
                receipt = confirmation(replace(ctx, trigger_msg_id=confirm_id), confirm_text)
                result['events_after_confirmation'] = conn.execute('SELECT count(*) FROM class_events').fetchone()[0]-1
                result['reminders_after_confirmation'] = conn.execute('SELECT count(*) FROM class_reminders').fetchone()[0]
                passed = (result['events_after_confirmation']==1 and result['reminders_after_confirmation']==2
                          and len(llm.calls)==before_calls and '执行回执' in receipt)
        result['passed'] = bool(passed)
        if not passed:
            result['reply_excerpt'] = (outcome.reply_text or '')[:500]
        return result
    except Exception as exc:
        return {'case':name, 'passed':False, 'error_type':type(exc).__name__,
                'status_code':getattr(exc,'status_code',None),
                'selected_tools':llm.calls if 'llm' in locals() else [],
                'plan_count':conn.execute('SELECT count(*) FROM class_action_plans').fetchone()[0],
                'reminders_before_confirmation':conn.execute('SELECT count(*) FROM class_reminders').fetchone()[0]}
    finally:
        conn.close()



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', nargs='+', default=['public_query','event_proposal','todo_proposal','unauthorized'])
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    real_llm = _build_llm_client()
    results = []
    with tempfile.TemporaryDirectory(prefix='oracle-tool-experiment-') as tmp:
        for name in args.cases:
            root = Path(tmp)/name
            root.mkdir()
            try:
                result = run_case(name, root, real_llm)
            except Exception as exc:
                result = {'case':name,'passed':False,'error_type':type(exc).__name__}
            results.append(result)
            Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print('EXPERIMENT '+json.dumps(result, ensure_ascii=True), flush=True)
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    raise SystemExit(0 if all(r['passed'] for r in results) else 2)


if __name__ == '__main__':
    main()
