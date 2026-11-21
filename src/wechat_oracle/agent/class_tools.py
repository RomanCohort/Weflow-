"""Actual classroom tools and per-turn receipts; retrieval is open to everyone."""
from dataclasses import dataclass, field

from .. import classroom
from ..class_actions import current_plan, officer_allowed, propose
from ..schedule import handle_schedule
from .tools import Tool, ToolError, ToolSpec, truncate_for_llm


@dataclass
class ClassSession:
    ctx: object
    receipts: list[str] = field(default_factory=list)
    proposed: bool = False

    @property
    def officer(self):
        return officer_allowed(self.ctx)


@dataclass
class QueryClassroom(Tool):
    session: ClassSession
    spec = ToolSpec('query_classroom',
        'Query actual stored classroom data in the current group. Open to every member. '
        'Use for today/week/schedule/todo questions; never invent stored records.',
        {'type': 'object', 'properties': {
            'query': {'type': 'string', 'enum': ['today', 'week', 'schedules', 'todos', 'todo_status', 'my_plan']},
            'item_id': {'type': 'integer', 'minimum': 1}}, 'required': ['query']})

    def call(self, args):
        ctx, query = self.session.ctx, args.get('query')
        if query == 'today':
            result = classroom.dashboard(ctx)
        elif query == 'week':
            result = classroom.dashboard(ctx, week=True)
        elif query == 'schedules':
            result = handle_schedule(ctx, 'list')
        elif query == 'todos':
            result = classroom.handle_todo(ctx, 'list')
        elif query == 'todo_status':
            item_id = args.get('item_id')
            if not isinstance(item_id, int) or isinstance(item_id, bool):
                raise ToolError('item_id must be an integer')
            try:
                result = classroom.handle_todo(ctx, f'status {item_id}')
            except ValueError as exc:
                result = '查询未完成：' + str(exc)
        elif query == 'my_plan':
            result = current_plan(ctx)
        else:
            raise ToolError('unsupported query')
        result = truncate_for_llm(result, limit=14000)
        self.session.receipts.append(result)
        return result


@dataclass
class ProposeClassAction(Tool):
    session: ClassSession
    spec = ToolSpec('propose_class_action',
        'Prepare ONE management proposal for an authorized class officer. This DOES NOT execute it. '
        'The program prints exact parameters and effects and requires the author to confirm in a later message. '
        'For revisions, query my_plan first and propose all revised parameters. '
        'Do not claim saved events or scheduled reminders when only a proposal exists. '
        'Unclear dates/times must be clarified before calling. All dates use UTC+08:00.',
        {'type': 'object', 'properties': {
            'kind': {'type': 'string', 'enum': ['event', 'todo', 'cancel', 'image', 'notice_confirm', 'notice_dismiss',
                                               'resource_add','resource_revise','resource_link']},
            'title': {'type': 'string'}, 'people': {'type': 'string'}, 'location': {'type': 'string'},
            'starts_at': {'type': 'string', 'description': 'ISO8601 with +08:00; event start or todo deadline'},
            'previous_day_time': {'type': 'string', 'description': 'HH:MM, default 19:00'},
            'event_day_time': {'type': 'string', 'description': 'HH:MM, default 07:00'},
            'remind_at_start': {'type':'boolean','description':'True only for explicit at-the-requested-time reminders'},
            'item_id': {'type': 'integer', 'description': 'Actual owned event ID for cancel, or draft ID for notice_confirm/notice_dismiss'},
            'prompt': {'type': 'string', 'description': 'For image only, max 1000 chars'},
            'course': {'type':'string','description':'Optional course for resource entries'},
            'body': {'type':'string','description':'Resource text or literal URL, max 2000 chars; no file reads'},
            'document_id': {'type':'integer','description':'Existing group document ID for resource_revise/resource_link'}}, 'required': ['kind']})

    def call(self, args):
        if self.session.proposed:
            raise ToolError('only one management proposal or notice extraction per turn')
        self.session.proposed = True
        try:
            result = propose(self.session.ctx, args)
        except (ValueError, TypeError, KeyError) as exc:
            result = '没有执行，方案未创建：' + str(exc)[:500]
        self.session.receipts.clear()
        self.session.receipts.append(result)
        return result


@dataclass
class ExtractClassNotice(Tool):
    session: ClassSession
    spec = ToolSpec('extract_class_notice',
        'Extract a notice into actual stored drafts for an authorized officer. '
        'Does not create events or reminders. Include the notice body, or an empty body to use the quoted message. '
        'The author must later use /notice confirm ID. No silent confirmation is permitted.',
        {'type': 'object', 'properties': {'body': {'type': 'string', 'maxLength': 16000}}, 'required': ['body']})

    def call(self, args):
        if not self.session.officer:
            raise ToolError('class officer authorization required; retrieval remains open')
        if self.session.proposed:
            raise ToolError('only one proposal or notice extraction per turn')
        body = args.get('body')
        if not isinstance(body, str) or len(body) > 16000:
            raise ToolError('notice body must be a string, at most 16000 chars')
        self.session.proposed = True
        if not body.strip() and not (self.session.ctx.quoted_text or '').strip():
            raise ToolError('provide a notice body or quote a notice')
        try:
            # Prefix untrusted data so it can NEVER become a command verb.
            result = classroom.handle_notice(self.session.ctx, '通知正文：\n' + body)
        except (ValueError, TypeError, KeyError) as exc:
            result = '通知未生成草稿：' + str(exc)[:500]
        self.session.receipts.clear()
        self.session.receipts.append(result)
        return result


def register_class_tools(tools, session):
    tools.register(QueryClassroom(session))
    if session.officer:
        tools.register(ProposeClassAction(session))
        tools.register(ExtractClassNotice(session))
