"""Native backend: thin wrapper around `orchestrator.chat_via_agent`.

Explicit mentions and replies require text; ambient triggers can stay silent.
Dispatcher call sites uniformly go through `AgentBackend.chat`,
which lets the openclaw backend slot in without touching dispatcher.

In native mode the in-process Phase A/B tool loop runs against the configured
WO_LLM_* provider; lurk also runs in-process via `orchestrator.chat_via_lurk`.
Switch to `WO_AGENT_BACKEND=openclaw` to delegate both chat and lurk to the
OpenClaw gateway (see F18 in CLAUDE.md / AGENTS.md).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...dispatcher import CommandContext


@dataclass
class NativeBackend:
    name: str = "native"

    def chat(
        self,
        *,
        ctx: "CommandContext",
        user_question: str,
        trigger_kind: str,
        reflection_enabled: bool | None = None,
    ) -> "AgentChatOutcome":
        from ..orchestrator import chat_via_agent
        session = None
        if trigger_kind in {"mention", "reply"}:
            from ...class_actions import confirmation, denied_management_request
            from ..backend import AgentChatOutcome
            from ..class_tools import ClassSession
            try:
                receipt = confirmation(ctx, user_question)
            except (ValueError, TypeError, KeyError) as exc:
                receipt = '没有执行：' + str(exc)[:500]
            if receipt is not None:
                return AgentChatOutcome(receipt, 'class-action: deterministic confirmation receipt')
            denied = denied_management_request(ctx, user_question)
            if denied is not None:
                return AgentChatOutcome(denied, 'class-action: program authorization denial; no model call')
            from ...calendar_intent import calendar_entry
            try:
                receipt=calendar_entry(ctx,user_question)
            except (ValueError,TypeError,KeyError) as exc:
                receipt='提醒尚未创建：'+str(exc)[:500]
            if receipt is not None:
                return AgentChatOutcome(receipt,'calendar: validated field extraction and actual proposal tool receipt')
            from ...rnaharness import should_route,chat as rnaharness_chat
            if should_route(ctx.group_id,trigger_kind,user_question):
                outcome=rnaharness_chat(ctx,user_question)
                if unsupported_management_claim(outcome.reply_text or ''):
                    return replace(outcome,reply_text='本轮 RNAHarness 没有调用群管理工具，未登记日程、待办或提醒。请使用已有群工具命令。')
                return outcome
            session = ClassSession(ctx)
            ctx = replace(ctx, llm=_DirectAddressLLM(ctx.llm, session), classroom_session=session)
        try:
            outcome = chat_via_agent(
                ctx=ctx,
                user_question=user_question,
                trigger_kind=trigger_kind,
                reflection_enabled=reflection_enabled,
            )
        except Exception as exc:
            if not session or not session.receipts:
                raise
            from ..backend import AgentChatOutcome
            outcome = AgentChatOutcome(None, f'classroom receipt recovered after {type(exc).__name__}')
        if session and session.receipts:
            if outcome.continuation_token:
                from ...db import transaction
                from ..continuation import cancel_planned_followups
                with transaction(ctx.conn):
                    cancel_planned_followups(ctx.conn, continuation_token=outcome.continuation_token,
                                            reason='classroom tool receipt; no unconfirmed followup')
            return replace(outcome, reply_text='\n\n'.join(dict.fromkeys(session.receipts)), continuation_token=None)
        if session and unsupported_management_claim(outcome.reply_text or ''):
            return replace(outcome, reply_text=(
                '本轮没有成功调用班级管理工具，因此没有登记或取消事项、保存资料，也没有新增提醒。'
                '请明确说明要做的操作；仍可使用 /schedule、/todo、/notice。'), continuation_token=None)
        return outcome


def unsupported_management_claim(text):
    return bool(re.search(r'已(?:经)?[^\n。]{0,24}(?:登记|安排[^\n。]{0,8}提醒|创建[^\n。]{0,8}(?:日程|待办)|'
                          r'取消[^\n。]{0,8}(?:日程|待办))|我会[^\n。]{0,24}(?:提醒|按时通知)|'
                          r'(?:到时|届时|到点)[^\n。]{0,12}(?:提醒你|通知你)|'
                          r'(?:登记|创建日程|安排提醒|保存资料)成功|已(?:经)?[^\n。]{0,12}设置[^\n。]{0,8}提醒|'
                          r'已(?:经)?[^\n。]{0,12}(?:保存|更新|存入)[^\n。]{0,8}(?:资料|课件)', text))

_DIRECT_REPLY_RULE = (
    "\n\nDirect-address policy: the current turn explicitly mentions or replies "
    "to the bot. Give a concise, non-empty text response. Do not call stay_silent "
    "or ignore a harmless joke. If you cannot help, explain briefly in text."
    " For stored class events, deadlines and dashboards, call query_classroom for actual records. "
    "Use search_knowledge for semantic notice/material/history lookup and get_knowledge to verify sources, "
    "current versions and linked event state. Never treat a historical message as a currently active schedule. "
    "Management requests must use propose_class_action when available; it only prepares a proposal. "
    "Never claim an event was saved, cancelled or a reminder scheduled without a successful tool receipt. "
    "The real user's later confirmation is handled by the program, never by a model-generated confirmation. "
    "If propose_class_action is absent, explain that natural-language management is not authorized; "
    "retrieval and lookup remain available."
)


class _DirectAddressLLM:
    """Per-turn adapter; ambient turns and reflection keep their original policy."""

    def __init__(self, inner, session=None):
        self._inner = inner
        self._session = session
        self._max_tokens = 1200

    def _receipt_response(self):
        from ...llm import ToolingResponse
        text = '\n\n'.join(dict.fromkeys(self._session.receipts))
        return ToolingResponse(content=text, tool_calls=[], assistant_message={'role':'assistant','content':text})

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete_with_tools(self, **kwargs):
        params = dict(kwargs)
        if self._session and self._session.proposed and self._session.receipts:
            # Stop after a real proposal result. Reflection/audit must see the
            # actual receipt, not a later model's invented execution summary.
            return self._receipt_response()
        specs = params.get("tools") or []
        from ...message_render import is_degenerate_outbound
        from ...llm import ToolingResponse
        fallback = lambda text: ToolingResponse(
            content=text, tool_calls=[],
            assistant_message={"role": "assistant", "content": text},
        )
        if not any(t.get("function", {}).get("name") == "stay_silent" for t in specs):
            response = self._inner.complete_with_tools(**params)
            if is_degenerate_outbound(response.content):
                text = "我收到你的消息了，但这次没能生成有效回答。可以换个说法再问我。"
                return fallback(text)
            return response
        params["max_tokens"] = min(int(params.get("max_tokens") or 1200), 1200)
        messages = [dict(m) for m in params["messages"]]
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = str(messages[0].get("content") or "") + _DIRECT_REPLY_RULE
        else:
            messages.insert(0, {"role": "system", "content": _DIRECT_REPLY_RULE})
        params["messages"] = messages
        params["tools"] = [
            t for t in specs if t.get("function", {}).get("name") != "stay_silent"
        ]
        if not params["tools"]:
            params["tool_choice"] = "none"
        response = self._inner.complete_with_tools(**params)
        if self._session and self._session.receipts and not response.tool_calls:
            return self._receipt_response()
        invalid_silence = any(c.name == "stay_silent" for c in response.tool_calls)
        if (not invalid_silence and not is_degenerate_outbound(response.content)
                and (response.tool_calls or (response.content or "").strip())):
            return response
        # Retry without tools, using the original conversation, not an unfulfilled call.
        params["tools"] = []
        params["tool_choice"] = "none"
        response = self._inner.complete_with_tools(**params)
        if (not response.tool_calls and (response.content or "").strip()
                and not is_degenerate_outbound(response.content)):
            return response
        text = "我收到你的消息了，但这次没能生成有效回答。可以换个说法再问我。"
        return fallback(text)
