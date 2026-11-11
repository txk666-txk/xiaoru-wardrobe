"""LangGraph orchestration for bounded digital-human styling turns."""
from __future__ import annotations

from copy import deepcopy
from contextvars import Context
import re
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from .planner import StylingDecision, PlannerError, parse_decision, public_context, failure_code


GROUP_WORDS = {'tops': ('上衣', '衬衫', 'T恤', '上装', '上面'),
               'bottoms': ('裤', '下装', '下半身'), 'outerwear': ('外套', '外衣'),
               'one-pieces': ('连衣裙',)}
CHEAPER = r'便宜(?:一[点些])?|实惠|平价'


def item_edits(text):
    keep, release, cheaper, replace = set(), set(), set(), set()
    for clause in re.split(r'[，,。；;！]', text):
        for group, words in GROUP_WORDS.items():
            if not any(word in clause for word in words):
                continue
            if re.search(r'取消保留|不用保留|不保留|不必保留|也(?:换|改)', clause):
                release.add(group)
            elif re.search(r'保留|留着|不换|不变|不用动|别动|照旧', clause):
                keep.add(group)
            if re.search(CHEAPER, clause):
                cheaper.add(group)
            if re.search(r'换(?:一?[个件条款种]|掉|下|成|个款)|另(?:一[件条款]|选)|也换', clause) and not re.search(r'不换|别换|不用换', clause):
                replace.add(group)
    return keep - release, release, cheaper, replace


@dataclass
class StylingOutcome:
    action: str = ''
    options: dict = field(default_factory=dict)
    observation: dict | None = None
    waiting: list = field(default_factory=list)
    executions: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    result: dict | None = None
    keep_ids: list = field(default_factory=list)
    used_model: bool = False
    fallback: bool = False
    fallback_reason: str = ''
    failed: bool = False
    workflow_engine: str = 'langgraph'
    graph_nodes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StylingRequest:
    """Trusted per-invocation input, never passed directly to the text model."""
    text: str
    owner: str
    attachment_id: str | None
    current: dict | None
    conversation: dict
    skip: bool
    opinion_only: bool
    model_only_followup: bool


class StylingGraphState(TypedDict):
    request: StylingRequest
    options: dict
    observation: dict | None
    photo_pending: bool
    model_active: bool
    decision_count: int
    tool_count: int
    action: str
    waiting: list
    executions: list
    audit: list[str]
    graph_nodes: list[str]
    keep_ids: list
    keep_groups: set[str]
    release_groups: set[str]
    cheaper_groups: set[str]
    replace_groups: set[str]
    used_model: bool
    fallback: bool
    fallback_reason: str
    failed: bool
    result: dict | None
    outcome: StylingOutcome


class StylingWorkflow:
    """Real graph execution; at most three decisions and two business tools.

    No checkpointer, automatic retries, remote tracing setup or global user state
    is installed here. Existing session ownership and turn locks remain in charge.
    """
    MAX_DECISIONS = 3
    MAX_TOOLS = 2

    def __init__(self, registry, planner=None):
        self.registry, self.planner = registry, planner
        graph = StateGraph(StylingGraphState)
        graph.add_node('decide', self._decide)
        graph.add_node('execute_tool', self._execute_tool)
        graph.add_node('step_limit', self._step_limit)
        graph.add_node('finalize', self._finalize)
        graph.add_edge(START, 'decide')
        graph.add_conditional_edges('decide', self._after_decision)
        graph.add_conditional_edges('execute_tool', self._after_tool)
        graph.add_edge('step_limit', 'finalize')
        graph.add_edge('finalize', END)
        self.graph = graph.compile()

    def run(self, *, text, owner, attachment_id, options, observation, current, state,
            skip=False, opinion_only=False, model_only_followup=False):
        request = StylingRequest(text, owner, attachment_id, deepcopy(current), deepcopy(state),
                                 skip, opinion_only, model_only_followup)
        previous = (request.current or {}).get('result', {}).get('outfits', [])
        known_ids = {item['product_id'] for item in previous[0]['items']} if previous else set()
        keep, release, cheaper, replace = item_edits(text)
        initial = {
            'request': request, 'options': deepcopy(options), 'observation': deepcopy(observation),
            'photo_pending': bool(attachment_id), 'model_active': self.planner is not None,
            'decision_count': 0, 'tool_count': 0, 'action': '', 'waiting': [],
            'executions': [], 'audit': [], 'graph_nodes': [],
            'keep_ids': [pid for pid in state.get('keep_product_ids', []) if pid in known_ids],
            'keep_groups': keep, 'release_groups': release, 'cheaper_groups': cheaper, 'replace_groups': replace,
            'used_model': False, 'fallback': False, 'fallback_reason': '', 'failed': False, 'result': None,
        }
        # Graph steps include routing/finalization and differ from model decisions.
        # Environment-wide tracing and inherited callbacks must not export owner,
        # body measurements, attachment IDs or the private conversation state.
        def invoke_private_graph():
            with tracing_context(enabled=False):
                return self.graph.invoke(initial, config={'recursion_limit': 12, 'callbacks': []})
        # A fresh context also excludes an outer explicit tracing_v2_enabled()
        # tracer. The caller's tracing/auth context is restored without mutation.
        final = Context().run(invoke_private_graph)
        return final['outcome']

    def _decide(self, state: StylingGraphState) -> dict:
        request = state['request']
        options = dict(state['options'])
        conversation = request.conversation
        missing = [key for key in ('scene', 'budget') if key not in options]
        ask_profile = (conversation.get('collected') and not conversation.get('personal_asked') and not request.skip and not (
            (options.get('season') or options.get('temperature') is not None) and
            (options.get('height_cm') or options.get('fit') or options.get('body_type'))))
        decision = (StylingDecision('inspect_outfit_photo') if state['photo_pending'] else
                    StylingDecision('handoff') if request.model_only_followup else
                    StylingDecision('opinion') if request.opinion_only else
                    StylingDecision('ask', question_fields=tuple(missing)) if missing else
                    StylingDecision('ask', question_fields=('profile',)) if ask_profile else
                    StylingDecision('recommend_outfit'))
        available = (['inspect_outfit_photo'] if state['photo_pending'] else
                     ['opinion'] if request.opinion_only else
                     ['ask', 'recommend_outfit'] if missing else ['recommend_outfit'] + (['ask'] if ask_profile else []))
        if not state['photo_pending']:
            available += ['pause', 'handoff']
            if state['observation'] and 'opinion' not in available:
                available.append('opinion')
        updates = {'decision_count': state['decision_count'] + 1, 'graph_nodes': state['graph_nodes'] + ['decide']}
        if state['model_active']:
            try:
                # Graph state contains trusted identity; the model sees only this allowlisted view.
                context = public_context(text=request.text, options=options, observation=state['observation'],
                    current=request.current, available=available, waiting=conversation.get('waiting', []),
                    personal_asked=bool(conversation.get('personal_asked')), photo_pending=state['photo_pending'])
                candidate = parse_decision(self.planner.decide(context), context['text'])
                if candidate.action not in available:
                    raise PlannerError('action_unavailable')
                proposed = {**options, **candidate.updates}
                if 'budget' in candidate.updates:
                    numbers = [float(n) for n in re.findall(r'(?<!\d)\d+(?:\.\d+)?', context['text'])]
                    if candidate.updates['budget'] not in numbers:
                        raise PlannerError('invented_budget')
                from .engine import parse_feedback
                explicit = parse_feedback(request.text)
                for key in candidate.updates:
                    if key in explicit and key != 'budget':
                        proposed[key] = explicit[key]
                if 'budget' in explicit and re.search(r'预算|不超过|以内|控制在', request.text):
                    proposed['budget'] = explicit['budget']
                if 'season' in candidate.updates and 'temperature' not in explicit:
                    proposed.pop('temperature', None)
                still_missing = [key for key in ('scene', 'budget') if key not in proposed]
                if candidate.action == 'recommend_outfit' and still_missing:
                    raise PlannerError('missing_requirements')
                if candidate.action == 'ask':
                    questions = set(candidate.question_fields)
                    if not questions or (still_missing and questions != set(still_missing)) or (
                            not still_missing and (questions != {'profile'} or not ask_profile)):
                        raise PlannerError('repeated_or_invalid_question')
                if candidate.action == 'opinion' and not state['observation']:
                    raise PlannerError('no_visual_evidence')
                decision = candidate
                updates.update(options=proposed, used_model=True,
                    audit=state['audit'] + ['ModelDecision: ' + decision.action])
                for key in ('keep_groups', 'release_groups', 'cheaper_groups', 'replace_groups'):
                    updates[key] = state[key] | set(getattr(candidate, key))
            except Exception as exc:
                reason = failure_code(exc)
                updates.update(model_active=False, fallback=True, fallback_reason=reason,
                    audit=state['audit'] + ['PlannerFallback: ' + reason + '；改用规则流程'])
        updates.update(action=decision.action,
                       waiting=list(decision.question_fields) if decision.action == 'ask' else [])
        return updates

    def _after_decision(self, state: StylingGraphState) -> Literal['execute_tool', 'finalize', 'step_limit']:
        if state['action'] in {'ask', 'opinion', 'pause', 'handoff'}:
            return 'finalize'
        if state['decision_count'] > self.MAX_DECISIONS or state['tool_count'] >= self.MAX_TOOLS:
            return 'step_limit'
        return 'execute_tool'

    def _execute_tool(self, state: StylingGraphState) -> dict:
        request, action = state['request'], state['action']
        previous = (request.current or {}).get('result', {}).get('outfits', [])
        items = previous[0]['items'] if previous else []
        keep_ids = list(state['keep_ids'])
        if action == 'inspect_outfit_photo':
            args = {'attachment_id': request.attachment_id, 'demo_session_id': request.owner}
        elif action == 'recommend_outfit':
            changed = state['cheaper_groups'] | state['replace_groups']
            released = state['release_groups'] | changed
            keep_ids = [pid for pid in keep_ids if not any(
                item['product_id'] == pid and item['group'] in released for item in items)]
            keep_groups = state['keep_groups'] - released
            keep_groups.update(item['group'] for item in items if changed and item['group'] not in changed)
            keep_ids = list(dict.fromkeys(keep_ids + [item['product_id'] for item in items if item['group'] in keep_groups]))
            caps = {item['group']: round(item['price'] - .01, 2) for item in items
                    if item['group'] in state['cheaper_groups'] and isinstance(item.get('price'), (int, float))}
            args = {'query': request.text, 'demo_session_id': request.owner, 'options': state['options'],
                    'observation': state['observation'], 'keep_product_ids': keep_ids, 'max_item_prices': caps,
                    'exclude_product_ids': [item['product_id'] for item in items if item['group'] in state['replace_groups']],
                    'max_total_price': round(previous[0]['total'] - .01, 2)
                        if previous and re.search(CHEAPER, request.text) and not state['cheaper_groups'] else None}
        else:
            # Never pass a model-supplied arbitrary tool name through the registry.
            raise ValueError('unsupported_styling_action')
        try:
            result = self.registry.execute(action, **args)
            if not isinstance(result, dict):
                raise ValueError('invalid_tool_result')
        except Exception:
            result = {'ok': False, 'error': 'tool_exception', 'message': '工具暂时不可用，请稍后再试。'}
        updates = {'tool_count': state['tool_count'] + 1, 'keep_ids': keep_ids,
                   'executions': state['executions'] + [(action, args, result)],
                   'audit': state['audit'] + ['Action: ' + action, 'Observation: ' + ('工具完成' if result.get('ok') else '工具失败，停止后续推荐')],
                   'graph_nodes': state['graph_nodes'] + ['execute_tool']}
        if not result.get('ok'):
            updates.update(failed=True, result=result)
        elif action == 'inspect_outfit_photo':
            updates.update(observation=result['data']['observation'], photo_pending=False)
        else:
            updates['result'] = result
        return updates

    def _after_tool(self, state: StylingGraphState) -> Literal['decide', 'finalize', 'step_limit']:
        if state['failed'] or state['action'] == 'recommend_outfit':
            return 'finalize'
        return 'step_limit' if state['decision_count'] >= self.MAX_DECISIONS else 'decide'

    def _step_limit(self, state: StylingGraphState) -> dict:
        return {'failed': True, 'audit': state['audit'] + ['StepLimit: 已到达执行上限'],
                'graph_nodes': state['graph_nodes'] + ['step_limit'],
                'result': {'ok': False, 'message': '本次已到达执行上限，请简短说明需要调整的衣服。'}}

    def _finalize(self, state: StylingGraphState) -> dict:
        nodes = state['graph_nodes'] + ['finalize']
        outcome = StylingOutcome(**{key: state[key] for key in (
            'action', 'options', 'observation', 'waiting', 'executions', 'audit', 'result',
            'keep_ids', 'used_model', 'fallback', 'fallback_reason', 'failed')}, graph_nodes=nodes)
        return {'graph_nodes': nodes, 'outcome': outcome}
