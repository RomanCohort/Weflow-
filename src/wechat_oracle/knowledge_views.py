"""Public evidence rendering and explicit group-scoped resource commands."""
import time
from datetime import datetime

from .class_actions import officer_allowed
from .db import transaction
from .knowledge import document, new_document, owned_event, revision, source
from .knowledge_search import search
from .schedule import TZ, _actor


def citation(rev):
    parts=[f"发起消息 #{rev['command_msg_id']}"] if rev['command_msg_id'] else []
    if rev['origin_msg_id']:
        parts.append(f"原始消息 #{rev['origin_msg_id']}")
    elif rev['origin_wx_id'] or rev['quote_text']:
        parts.append('原文未解析到归档记录，仅保留引用标识/片段')
    return '；'.join(parts) or '来源未记录'


def show(conn, group, doc_id, *, version=None, body=True):
    doc,rev,links=document(conn,group,doc_id,version=version)
    label={'message':'群消息原文（不等于已登记事项）','notice':'已确认通知','resource':'资料条目','note':'归档笔记（结论，可检索）'}.get(doc['kind'], doc['kind'])
    current=doc['current_revision_id']==rev['revision_id']
    lines=[f"D{doc_id} v{rev['version']}【{label}；{'当前' if current else '历史'}版本】{rev['title']}",
           f"课程：{rev['course'] or '未分类'}",'来源：'+citation(rev)]
    if body:
        text=rev['body']
        lines.append(text[:7000]+('\n（正文展示已截断，原归档保留）' if len(text)>7000 else ''))
        if doc['kind']=='notice' and rev['origin_text'] and rev['origin_text']!=text:
            lines.append('原始消息快照：\n'+rev['origin_text'][:4000]+('\n（快照展示已截断）' if len(rev['origin_text'])>4000 else ''))
    for link in links:
        state='已取消/已被修订替换' if link['cancelled'] else ('已过时间' if link['starts_at']<=time.time() else '有效')
        date=datetime.fromtimestamp(link['starts_at'],TZ).strftime('%Y-%m-%d %H:%M')
        lines.append(f"关联事项 #{link['event_id']}：{link['title']}，{date}，{state}（{link['role']}，关联时v{link['linked_version']}）")
    if not links:
        lines.append('没有关联的已登记事项；检索命中不会自动创建日程或待办。')
    return '\n'.join(lines)


def find(conn, group, query, limit=8):
    hits,mode=search(conn,group,query,limit)
    if not hits:
        return f'未找到匹配的已索引资料（{mode}）。后台索引未完成或内容未归档时可能暂时查不到。'
    blocks=[f'检索方式：{mode}。以下为相关候选，时间与状态按数据库当前记录展示。']
    for hit in hits:
        # Re-read canonical record after ranking; stale vectors are not evidence.
        try:
            _,current,_=document(conn,group,hit['doc_id'])
            block=show(conn,group,hit['doc_id'],body=False)
        except ValueError:
            continue
        snippet=hit['text'] if current['version']==hit['version'] else current['body']
        blocks.append(block+'\n当前版本片段：'+snippet[:550]+f"\n完整内容：/kb show D{hit['doc_id']}")
    return '\n\n'.join(blocks)


def event_sources(ctx, item_id):
    event=ctx.conn.execute('SELECT * FROM class_events WHERE event_id=? AND group_id=?',(item_id,ctx.group_id)).fetchone()
    if not event:
        raise ValueError('本群没有这条事项。')
    links=ctx.conn.execute('SELECT l.*,r.version FROM knowledge_event_links l '
                           'JOIN knowledge_documents d ON d.doc_id=l.doc_id '
                           'JOIN knowledge_revisions r ON r.revision_id=l.revision_id '
                           'WHERE l.event_id=? AND d.group_id=?',(item_id,ctx.group_id)).fetchall()
    lines=[f"事项 #{item_id}：{event['title']}（{'已取消/替换' if event['cancelled'] else '未取消'}）"]
    for link in links:
        lines.append(f"关联时引用 D{link['doc_id']} v{link['version']}（{link['role']}）；以下为该资料当前状态：\n"
                     +show(ctx.conn,ctx.group_id,link['doc_id'],body=False))
    return '\n\n'.join(lines) if links else lines[0]+'\n尚无来源/资料关联。历史事项不能凭空补出来源。'


def event_source_brief(conn,group,item_id):
    from .knowledge import ready
    if not ready(conn):return '来源：未保存可追溯关联，不能确认原始通知。'
    row=conn.execute("SELECT r.*,d.doc_id,l.role FROM knowledge_event_links l "
                     "JOIN knowledge_documents d ON d.doc_id=l.doc_id JOIN knowledge_revisions r ON r.revision_id=l.revision_id "
                     "WHERE l.event_id=? AND d.group_id=? AND l.role IN ('notice','request') "
                     "ORDER BY CASE l.role WHEN 'notice' THEN 0 ELSE 1 END LIMIT 1",(item_id,group)).fetchone()
    if not row:return '来源：关联缺失，不能确认原始通知。'
    label='通知原文' if row['role']=='notice' and row['origin_msg_id'] else '用户登记请求'
    if not row['origin_msg_id'] and row['quote_text']:label='仅有引用片段'
    original=row['origin_text'] or row['quote_text']
    if not original:
        return f"来源：D{row['doc_id']} v{row['version']}；{citation(row)}；原文正文缺失。"
    excerpt=original[:260]
    return (f"来源：{label}，D{row['doc_id']} v{row['version']}；{citation(row)}\n"
            f"原文摘录：{excerpt}"+('…（已截断，完整内容见资料来源）' if len(original)>260 else ''))


def resource_fields(course,title,body):
    if (not all(isinstance(x,str) for x in (course,title,body)) or not title.strip() or len(title)>120
            or not body.strip() or len(body)>16000 or len(course)>100):
        raise ValueError('标题限1至120字，正文限1至16000字，课程限100字。')
    return course.strip(),title.strip(),body.strip()


def save_resource(ctx,course,title,body,*,doc_id=None,request_msg_id=None):
    """Caller owns the transaction, including confirmed natural-language plans."""
    if not officer_allowed(ctx):
        raise ValueError('资料写入需先配置本群班委名单；检索与阅读始终开放。')
    course,title,body=resource_fields(course,title,body)
    actor=_actor(ctx);provenance=source(ctx,body,request_msg_id=request_msg_id)
    if doc_id is None:
        doc=new_document(ctx.conn,ctx.group_id,'resource',actor,f'resource:{request_msg_id or ctx.trigger_msg_id}')
    else:
        doc,_,_=document(ctx.conn,ctx.group_id,doc_id)
        if doc['kind']!='resource' or doc['owner']!=actor:
            raise ValueError('只能修订你在本群创建的资料条目。')
    rid=revision(ctx.conn,doc,title,body,provenance,course=course,publish=True)
    version=ctx.conn.execute('SELECT version FROM knowledge_revisions WHERE revision_id=?',(rid,)).fetchone()[0]
    return doc,version


def write_resource(ctx, content, *, doc_id=None):
    parts=content.split('|',2)
    if len(parts)!=3:
        raise ValueError('格式：课程 | 标题 | 正文或链接；不会自动下载文件或抓取链接。')
    course,title,body=(x.strip() for x in parts)
    with transaction(ctx.conn):
        doc,version=save_resource(ctx,course,title,body,doc_id=doc_id)
    return f"已保存资料 D{doc['doc_id']} v{version}，旧版本保留，语义索引在后台更新。\n"+show(ctx.conn,ctx.group_id,doc['doc_id'])


def attach_resource(ctx,item_id,doc_id):
    if not officer_allowed(ctx):
        raise ValueError('关联资料需本群班委权限。')
    owned_event(ctx,item_id)
    doc,rev,_=document(ctx.conn,ctx.group_id,doc_id)
    ctx.conn.execute('INSERT OR IGNORE INTO knowledge_event_links VALUES(?,?,?,?)',
                     (item_id,doc['doc_id'],rev['revision_id'],'resource'))


def link_resource(ctx, item_id, doc_id):
    with transaction(ctx.conn):
        attach_resource(ctx,item_id,doc_id)
    return '资料关联已记录（已有的关联不会重复创建）。\n'+event_sources(ctx,item_id)


def number(text):
    return int(text.lstrip('Dd#'))


def handle_kb(ctx, text):
    parts=text.split(maxsplit=1);action=parts[0].lower() if parts else 'help';tail=parts[1] if len(parts)>1 else ''
    if action=='find':return find(ctx.conn,ctx.group_id,tail)
    if action=='show':
        fields=tail.split()
        if not fields:raise ValueError('用法：/kb show D3 [v1]')
        return show(ctx.conn,ctx.group_id,number(fields[0]),version=int(fields[1].lstrip('vV')) if len(fields)>1 else None)
    if action=='event':return event_sources(ctx,number(tail))
    if action=='add':return write_resource(ctx,tail)
    if action=='revise':
        doc_id,content=tail.split(maxsplit=1)
        return write_resource(ctx,content,doc_id=number(doc_id))
    if action=='link':
        item_id,doc_id=tail.split()
        return link_resource(ctx,number(item_id),number(doc_id))
    if action=='versions':
        doc_id=number(tail);document(ctx.conn,ctx.group_id,doc_id)
        rows=ctx.conn.execute('SELECT * FROM knowledge_revisions WHERE doc_id=? AND published=1 ORDER BY version DESC LIMIT 20',(doc_id,)).fetchall()
        return '\n'.join(f"D{doc_id} v{r['version']}：{r['title']}；{citation(r)}" for r in rows)
    return ('/kb find <问题>：混合检索本群资料与消息\n/kb show D3：读当前版本\n/kb versions D3：历史版本\n'
            '/kb event 3：事项来源与资料\n/kb add 课程 | 标题 | 正文或链接\n'
            '/kb revise D3 课程 | 标题 | 新正文\n/kb link 事项编号 D3：关联资料。写入仅限班委，文件原件暂不读取。')


def install_knowledge_command(base,register,parse_error,result):
    @register
    class KnowledgeCommand(base):
        name='kb'
        usage='/kb find/show/versions/event/add/revise/link <参数>'
        description='检索本群来源可追溯的资料与消息，查看版本和事项关联；写入需班委权限'
        examples=['/kb find 小组汇报的PPT要求','/kb show D3','/kb event 3']
        def __init__(self,text):self.text=text
        @classmethod
        def parse(cls,args):return cls(args.strip())
        def execute(self,ctx):
            try:text=handle_kb(ctx,self.text)
            except (ValueError,TypeError,KeyError) as exc:text='资料操作未完成：'+str(exc)[:500]
            return result(stdout=text,chat=text,summary='kb')
