"""Public, group-bound knowledge retrieval; results come from canonical records."""
from dataclasses import dataclass

from ..knowledge_views import event_sources, find, show
from .tools import Tool, ToolError, ToolSpec


@dataclass
class SearchKnowledge(Tool):
    session: object
    spec=ToolSpec('search_knowledge',
        'Search this group historical messages, confirmed notices and saved course materials using hybrid '
        'lexical/Qwen embeddings retrieval. Everyone may search. Results include actual sources, versions '
        'and current linked event state. Matching a message never means a reminder exists.',
        {'type':'object','properties':{'query':{'type':'string','maxLength':500},
                                     'limit':{'type':'integer','minimum':1,'maximum':20}},'required':['query']})
    def call(self,args):
        ctx=self.session.ctx
        try:result=find(ctx.conn,ctx.group_id,args.get('query'),args.get('limit',8))
        except (ValueError,TypeError) as exc:raise ToolError(str(exc)) from exc
        self.session.receipts.append(result)
        return result


@dataclass
class GetKnowledge(Tool):
    session: object
    spec=ToolSpec('get_knowledge',
        'Read an actual stored document and source/version/state, or inspect sources linked to an event. '
        'Current version is the default. Historical versions are explicitly labeled; unconfirmed versions are hidden.',
        {'type':'object','properties':{'document_id':{'type':'integer'},'version':{'type':'integer'},
                                     'event_id':{'type':'integer'} }})
    def call(self,args):
        ctx=self.session.ctx
        try:
            if 'event_id' in args:
                result=event_sources(ctx,int(args['event_id']))
            else:
                result=show(ctx.conn,ctx.group_id,int(args['document_id']),version=args.get('version'))
        except (ValueError,TypeError,KeyError) as exc:raise ToolError(str(exc)) from exc
        self.session.receipts.append(result)
        return result


def register_knowledge_tools(tools,session):
    # Phase A may already have registered the plain group-wide search.
    if tools.get('search_knowledge') is None:
        tools.register(SearchKnowledge(session))
    if tools.get('get_knowledge') is None:
        tools.register(GetKnowledge(session))
