"""Durable, actor/group-bound management proposals with deterministic receipts.

The model may propose parameters but cannot confirm them. Confirmation is parsed
from the real user's current message. Stored parameters are executed unchanged;
no second model interpretation occurs during confirmation.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta

from loguru import logger

from .config import settings
from .db import transaction
from .schedule import TZ, _actor, describe, persist_event, registration_text, validate_event

SCHEMA = """
CREATE TABLE IF NOT EXISTS class_action_plans (
 plan_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, creator TEXT NOT NULL,
 source_msg_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
 preview TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', result TEXT, exec_msg_id INTEGER,
 UNIQUE(group_id, source_msg_id)
);
CREATE INDEX IF NOT EXISTS class_plans_actor ON class_action_plans(group_id,creator,status);
"""


def init_actions(conn):
    conn.executescript(SCHEMA)


def officer_allowed(ctx):
    path = settings.data_dir / 'class_officers.json'
    try:
        if not path.exists():
            return False
        if path.stat().st_size > 65536:
            raise ValueError('oversized officer configuration')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        groups = data['groups']
        if not isinstance(groups, dict):
            raise ValueError('groups must be an object')
        members = groups.get(ctx.group_id, [])
        if not isinstance(members, list) or not all(isinstance(x, str) and x for x in members):
            raise ValueError('members must be account IDs')
        return _actor(ctx) in members
    except (OSError, ValueError, TypeError, KeyError):
        logger.warning('class officer configuration unavailable or invalid; management proposals disabled')
        return False


def denied_management_request(ctx, text):
    """Reject clear unauthorized writes before asking a model to improvise.

    Retrieval/question wording always keeps the existing public agent path.
    Mixed/ambiguous requests still use tools, whose own authorization is final.
    """
    if officer_allowed(ctx):
        return None
    from .calendar_intent import calendar_write_request
    if calendar_write_request(text):
        return '没有执行：当前账号不在本群班委名单，不能通过自然语言创建群提醒。检索和查询仍然开放。'
    if re.search(r'查|找|搜|检索|总结|看|是否|有没有|有哪些|什么|哪里|几点|何时', text):
        return None
    if re.search(r'登记|新建.*(?:日程|待办)|创建.*(?:日程|待办)|记下.*(?:截止|提交|交|活动)|'
                 r'(?:安排|设置).*提醒|提醒(?:我|全班)|取消.*(?:日程|待办|提醒)|'
                 r'(?:保存|存入|新增|修订|更新|关联).*(?:资料|资料库|课件)', text):
        return ('没有执行：你尚未列入本群班委名单，不能通过自然语言登记、取消或安排群提醒。'
                '检索、查找、已登记事项和看板查询仍向所有人开放；原有 / 命令继续可用。')
    return None


def _owned_item(ctx, item_id):
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise ValueError('事项编号必须为整数。')
    row = ctx.conn.execute(
        'SELECT * FROM class_events WHERE event_id=? AND group_id=? AND creator=? AND cancelled=0',
        (item_id, ctx.group_id, _actor(ctx)),
    ).fetchone()
    if not row:
        raise ValueError('只能操作你在本群创建的有效事项。')
    return row


def _owned_draft(ctx, draft_id, now):
    if not isinstance(draft_id, int) or isinstance(draft_id, bool):
        raise ValueError('草稿编号必须为整数。')
    row = ctx.conn.execute('SELECT * FROM class_notice_drafts WHERE draft_id=? AND group_id=? AND creator=?',
                           (draft_id, ctx.group_id, _actor(ctx))).fetchone()
    if not row or row['status'] != 'pending' or row['expires_at'] <= now.timestamp():
        raise ValueError('找不到你在本群创建的有效待确认草稿。')
    return row


def prepare(ctx, args, now):
    kind = args.get('kind')
    if kind in {'event', 'todo'}:
        data, start, reminders = validate_event(args, now)
        payload = {k: data[k] for k in ('title', 'people', 'location', 'starts_at')}
        payload.update(previous_day_time=data.get('previous_day_time', '19:00'),
                       event_day_time=data.get('event_day_time', '07:00'),
                       remind_at_start=data.get('remind_at_start',False))
        verb = '日程' if kind == 'event' else '截止待办'
        preview = (f"准备调用：登记{verb}\n事项：{payload['title']}\n"
                   f"{'开始' if kind == 'event' else '截止'}：{start:%Y-%m-%d %H:%M}（北京时间）\n"
                   f"人物：{payload['people']}\n地点：{payload['location']}")
        for label, due in reminders:
            suffix = '（已过，跳过）' if label == '前一天' and due <= now else '（确认后尽快补发）' if due <= now else ''
            preview += f'\n{label}提醒：{due:%m-%d %H:%M}{suffix}'
        preview += '\n预期效果：保存到本群数据库，并安排上述群提醒。'
    elif kind == 'cancel':
        row = _owned_item(ctx, args.get('item_id'))
        payload = {k: row[k] for k in ('event_id', 'title', 'starts_at', 'kind')}
        preview = '准备调用：取消事项\n' + describe(row) + '\n预期效果：保留记录、取消该事项后续待发提醒。'
    elif kind in {'notice_confirm', 'notice_dismiss'}:
        row = _owned_draft(ctx, args.get('item_id'), now)
        data = json.loads(row['payload'])
        _, normalized, event_preview = prepare(ctx, {**data, 'kind': row['kind']}, now)
        payload = {'draft_id': row['draft_id'], 'draft_payload': row['payload'],
                   'event': normalized, 'event_kind': row['kind']}
        preview = (f"准备确认通知草稿 #{row['draft_id']}\n" + event_preview if kind == 'notice_confirm'
                   else f"准备放弃通知草稿 #{row['draft_id']}：{data['title']}\n预期效果：不登记事项、不安排提醒。")
        if kind=='notice_confirm' and 'replaces_event_id' in row.keys() and row['replaces_event_id']:
            preview+=f"\n确认将替换旧事项 #{row['replaces_event_id']} 并取消其待发提醒，历史保留。"
    elif kind in {'resource_add','resource_revise'}:
        from .knowledge import document
        from .knowledge_views import resource_fields
        course,title,body=resource_fields(args.get('course',''),args.get('title'),args.get('body'))
        if len(body)>2000:
            raise ValueError('自然语言资料方案正文限2000字，以便完整核对；长文可用 /kb add。')
        payload={'course':course,'title':title,'body':body}
        if kind=='resource_revise':
            doc,old,_=document(ctx.conn,ctx.group_id,args.get('document_id'))
            if doc['kind']!='resource' or doc['owner']!=_actor(ctx):
                raise ValueError('只能修订你在本群创建的资料。')
            payload.update(document_id=doc['doc_id'],previous_revision_id=old['revision_id'])
        preview=(f"准备调用：{'新建' if kind=='resource_add' else '修订'}资料条目\n课程：{course or '未分类'}"
                 f"\n标题：{title}\n正文：{body}\n预期效果：保存到本群资料库，旧版本保留，更新语义索引；不会新建提醒。")
    elif kind=='resource_link':
        from .knowledge import document
        target=_owned_item(ctx,args.get('item_id'))
        doc,rev,_=document(ctx.conn,ctx.group_id,args.get('document_id'))
        payload={'item_id':target['event_id'],'document_id':doc['doc_id'],'previous_revision_id':rev['revision_id']}
        preview=(f"准备调用：关联资料\n事项 #{target['event_id']}：{target['title']}"
                 f"\n资料 D{doc['doc_id']} v{rev['version']}：{rev['title']}\n预期效果：记录资料关联，不修改事项时间或提醒。")
    elif kind == 'image':
        prompt = args.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000:
            raise ValueError('图片描述需要1至1000字。')
        payload = {'prompt': prompt.strip()}
        preview = f'准备调用：Krea2 生图\n描述：{prompt}\n预期效果：生成1张1024×1024图片，8步采样，作为附件发回本群。'
    else:
        raise ValueError('不支持这种管理方案类型，请使用现有日程、待办、通知、资料或生图工具。')
    return kind, payload, preview


def propose(ctx, args):
    if not officer_allowed(ctx):
        raise ValueError('你尚未列入本群班委名单；检索和查询仍可正常使用。')
    now, actor = datetime.now(TZ), _actor(ctx)
    kind, payload, preview = prepare(ctx, args, now)
    with transaction(ctx.conn):
        existing = ctx.conn.execute(
            'SELECT * FROM class_action_plans WHERE group_id=? AND source_msg_id=?',
            (ctx.group_id, ctx.trigger_msg_id),
        ).fetchone()
        if existing:
            return existing['preview'] if existing['status'] == 'pending' else '这条请求已处理，没有重复创建方案。'
        ctx.conn.execute("UPDATE class_action_plans SET status='superseded' WHERE group_id=? "
                         "AND creator=? AND status='pending'", (ctx.group_id, actor))
        plan_id = 'P' + uuid.uuid4().hex[:10]
        expiry = now + timedelta(minutes=10)
        preview += (f'\n方案：{plan_id}（有效至 {expiry:%H:%M}）\n尚未执行。'
                    f'\n请本人 @机器人发送“确认执行 {plan_id}”，或“取消执行 {plan_id}”。'
                    '\n要修改时请重新说明，旧方案将作废。')
        ctx.conn.execute(
            'INSERT INTO class_action_plans(plan_id,group_id,creator,source_msg_id,kind,payload,preview,'
            'created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (plan_id, ctx.group_id, actor, ctx.trigger_msg_id, kind, json.dumps(payload, ensure_ascii=False),
             preview, now.timestamp(), expiry.timestamp()),
        )
    return preview


def current_plan(ctx):
    row = ctx.conn.execute(
        "SELECT preview FROM class_action_plans WHERE group_id=? AND creator=? AND status='pending' "
        'AND expires_at>? ORDER BY created_at DESC LIMIT 1',
        (ctx.group_id, _actor(ctx), datetime.now(TZ).timestamp()),
    ).fetchone()
    return row['preview'] if row else '你在本群没有有效的待确认管理方案。'


def confirmation(ctx, text):
    match = re.fullmatch(r'\s*(确认执行|取消执行)(?:\s+(P[0-9a-fA-F]{10}))?\s*[。！!]?\s*', text)
    if not match:
        return None
    if not officer_allowed(ctx):
        return '没有执行：你尚未列入本群班委名单。检索和查询仍向所有人开放。'
    now, actor = datetime.now(TZ), _actor(ctx)
    with transaction(ctx.conn):
        if match[2]:
            row = ctx.conn.execute('SELECT * FROM class_action_plans WHERE plan_id=? AND group_id=? AND creator=?',
                                   ('P' + match[2][1:].lower(), ctx.group_id, actor)).fetchone()
        else:
            row = ctx.conn.execute("SELECT * FROM class_action_plans WHERE group_id=? AND creator=? "
                                   "AND status='pending' ORDER BY created_at DESC LIMIT 1", (ctx.group_id, actor)).fetchone()
        if not row:
            return '没有找到你在本群的待确认方案，请先提出操作请求。'
        if row['status'] == 'done':
            return '该方案此前已执行，没有重复操作。\n' + row['result']
        if row['status'] != 'pending':
            return f"该方案状态为 {row['status']}，本次没有执行；执行中的方案不会自动重试。"
        if row['expires_at'] <= now.timestamp():
            ctx.conn.execute("UPDATE class_action_plans SET status='expired' WHERE plan_id=?", (row['plan_id'],))
            return '方案已超过10分钟有效期，没有执行。请重新提出请求。'
        if match[1] == '取消执行':
            ctx.conn.execute("UPDATE class_action_plans SET status='cancelled' WHERE plan_id=?", (row['plan_id'],))
            return '已取消待确认方案，没有修改日程、待办或提醒。'
        payload = json.loads(row['payload'])
        if row['kind'] in {'event', 'todo', 'notice_confirm'}:
            draft = None
            if row['kind'] == 'notice_confirm':
                draft = _owned_draft(ctx, payload['draft_id'], now)
                if draft['payload'] != payload['draft_payload']:
                    raise ValueError('通知草稿已改变，请重新核对方案。')
                from .knowledge import validate_replacement
                validate_replacement(ctx,draft)
            data, start, reminders = validate_event(payload['event'] if draft else payload, now)
            if any(row['created_at'] < due.timestamp() <= now.timestamp() for _, due in reminders):
                raise ValueError('方案中的提醒时间已到，请重新提出请求并核对补发安排。')
            event = persist_event(ctx, data, start, reminders, actor, now,
                                  payload['event_kind'] if draft else row['kind'],request_msg_id=row['source_msg_id'])
            if draft:
                from .knowledge import activate_notice
                activate_notice(ctx,draft,event)
                ctx.conn.execute("UPDATE class_notice_drafts SET status='confirmed',event_id=? WHERE draft_id=?",
                                 (event['event_id'], draft['draft_id']))
            result = registration_text(event, reminders, now)
        elif row['kind'] == 'notice_dismiss':
            draft = _owned_draft(ctx, payload['draft_id'], now)
            if draft['payload'] != payload['draft_payload']:
                raise ValueError('通知草稿已改变，请重新核对方案。')
            ctx.conn.execute("UPDATE class_notice_drafts SET status='dismissed' WHERE draft_id=?", (draft['draft_id'],))
            result = f"已放弃草稿 #{draft['draft_id']}，没有登记事项或安排提醒。"
        elif row['kind'] == 'cancel':
            target = _owned_item(ctx, payload['event_id'])
            if any(target[k] != payload[k] for k in ('title', 'starts_at', 'kind')):
                raise ValueError('事项内容已发生变化，请重新核对取消方案。')
            ctx.conn.execute('UPDATE class_events SET cancelled=1 WHERE event_id=?', (target['event_id'],))
            ctx.conn.execute("UPDATE class_reminders SET status='cancelled' WHERE event_id=? AND status='pending'",
                             (target['event_id'],))
            result = f"已取消事项 #{target['event_id']} 及其待发提醒，原记录保留。"
        elif row['kind'] in {'resource_add','resource_revise','resource_link'}:
            from .knowledge import document
            from .knowledge_views import attach_resource,save_resource
            if row['kind']!='resource_add':
                _,current,_=document(ctx.conn,ctx.group_id,payload['document_id'])
                if current['revision_id']!=payload['previous_revision_id']:
                    raise ValueError('资料版本已变化，请重新核对方案。')
            if row['kind']=='resource_link':
                attach_resource(ctx,payload['item_id'],payload['document_id'])
                result=f"已关联事项 #{payload['item_id']} 与资料 D{payload['document_id']}，提醒未改变。"
            else:
                doc,version=save_resource(ctx,payload['course'],payload['title'],payload['body'],
                                         doc_id=payload.get('document_id'),request_msg_id=row['source_msg_id'])
                result=f"已保存资料 D{doc['doc_id']} v{version}，后台索引将更新；没有创建提醒。"
        elif row['kind'] == 'image':
            if ctx.attachment_sender is None:
                raise ValueError('附件发送通道不可用，没有开始生图。')
            ctx.conn.execute("UPDATE class_action_plans SET status='executing',exec_msg_id=? WHERE plan_id=?",
                             (ctx.trigger_msg_id, row['plan_id']))
            result = None
        else:
            raise ValueError('方案类型无效，没有执行。')
        if result is not None:
            ctx.conn.execute("UPDATE class_action_plans SET status='done',result=?,exec_msg_id=? WHERE plan_id=?",
                             (result, ctx.trigger_msg_id, row['plan_id']))
    if result is None:
        from .agent.krea2 import GenerateGroupImageTool
        try:
            receipt = GenerateGroupImageTool(ctx.group_id, ctx.group_name, ctx.attachment_sender).call(payload)
            result = '图片已生成并发回本群。\n工具回执：' + receipt
            status = 'done'
        except Exception as exc:
            result = f'生图未确认成功（{type(exc).__name__}）。可能已提交生成任务，不自动重试。'
            status = 'failed'
        ctx.conn.execute('UPDATE class_action_plans SET status=?,result=? WHERE plan_id=?',
                         (status, result, row['plan_id']))
    return f"执行回执 {row['plan_id']}：\n" + result
