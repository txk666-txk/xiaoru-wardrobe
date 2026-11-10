"""Bounded model/observe/tool loop shared by digital-human styling turns."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

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


class StylingWorkflow:
    """At most two tools and three decisions. Transaction tools are never exposed."""
    def __init__(self, registry, planner=None):
        self.registry, self.planner = registry, planner

    def run(self, *, text, owner, attachment_id, options, observation, current, state,
            skip=False, opinion_only=False, model_only_followup=False):
        out = StylingOutcome(options=dict(options), observation=observation)
        photo_pending = bool(attachment_id)
        model = self.planner
        keep_groups, release_groups, cheaper_groups, replace_groups = item_edits(text)
        previous = (current or {}).get('result', {}).get('outfits', [])
        items = previous[0]['items'] if previous else []
        known_ids = {item['product_id'] for item in items}
        out.keep_ids = [pid for pid in state.get('keep_product_ids', []) if pid in known_ids]
        for _ in range(3):
            missing = [key for key in ('scene', 'budget') if key not in out.options]
            ask_profile = (state.get('collected') and not state.get('personal_asked') and not skip and not (
                (out.options.get('season') or out.options.get('temperature') is not None) and
                (out.options.get('height_cm') or out.options.get('fit') or out.options.get('body_type'))))
            fallback = (StylingDecision('inspect_outfit_photo') if photo_pending else
                        StylingDecision('handoff') if model_only_followup else
                        StylingDecision('opinion') if opinion_only else
                        StylingDecision('ask', question_fields=tuple(missing)) if missing else
                        StylingDecision('ask', question_fields=('profile',)) if ask_profile else
                        StylingDecision('recommend_outfit'))
            available = (['inspect_outfit_photo'] if photo_pending else
                         ['opinion'] if opinion_only else
                         ['ask', 'recommend_outfit'] if missing else ['recommend_outfit'] + (['ask'] if ask_profile else []))
            if not photo_pending:
                available += ['pause', 'handoff']
                if out.observation and 'opinion' not in available:
                    available.append('opinion')
            decision = fallback
            if model is not None:
                try:
                    context = public_context(text=text, options=out.options, observation=out.observation,
                        current=current, available=available, waiting=state.get('waiting', []),
                        personal_asked=bool(state.get('personal_asked')), photo_pending=photo_pending)
                    candidate = parse_decision(model.decide(context), context['text'])
                    if candidate.action not in available:
                        raise PlannerError('action_unavailable')
                    proposed = {**out.options, **candidate.updates}
                    # Numeric budgets must occur in the cited user turn, never be invented.
                    if 'budget' in candidate.updates:
                        numbers = [float(n) for n in re.findall(r'(?<!\d)\d+(?:\.\d+)?', context['text'])]
                        if candidate.updates['budget'] not in numbers:
                            raise PlannerError('invented_budget')
                    # Deterministically parsed explicit facts win over model reinterpretations.
                    from .engine import parse_feedback
                    explicit = parse_feedback(text)
                    for key in candidate.updates:
                        if key in explicit and key != 'budget':
                            proposed[key] = explicit[key]
                    if 'budget' in explicit and re.search(r'预算|不超过|以内|控制在', text):
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
                    if candidate.action == 'opinion' and not out.observation:
                        raise PlannerError('no_visual_evidence')
                    out.options = proposed
                    keep_groups.update(candidate.keep_groups)
                    release_groups.update(candidate.release_groups)
                    cheaper_groups.update(candidate.cheaper_groups)
                    replace_groups.update(candidate.replace_groups)
                    decision = candidate
                    out.used_model = True
                    out.audit.append('ModelDecision: ' + decision.action)
                except Exception as exc:
                    # Do not echo provider errors, generated content, or credentials.
                    out.fallback = True
                    out.fallback_reason = failure_code(exc)
                    out.audit.append('PlannerFallback: ' + out.fallback_reason + '；改用规则流程')
                    model = None
            out.action = decision.action
            if decision.action == 'ask':
                out.waiting = list(decision.question_fields)
                return out
            if decision.action in {'opinion', 'pause', 'handoff'}:
                return out
            if decision.action == 'inspect_outfit_photo':
                args = {'attachment_id': attachment_id, 'demo_session_id': owner}
            else:
                # All product IDs come from this owner's verified current outfit.
                # A new request for a cheaper item supersedes its old retained ID.
                changed = cheaper_groups | replace_groups
                released = release_groups | changed
                out.keep_ids = [pid for pid in out.keep_ids if not any(
                    item['product_id'] == pid and item['group'] in released for item in items)]
                keep_groups -= released
                keep_groups.update(item['group'] for item in items if changed and item['group'] not in changed)
                out.keep_ids = list(dict.fromkeys(out.keep_ids + [item['product_id'] for item in items if item['group'] in keep_groups]))
                caps = {item['group']: round(item['price'] - .01, 2) for item in items
                        if item['group'] in cheaper_groups and isinstance(item.get('price'), (int, float))}
                args = {'query': text, 'demo_session_id': owner, 'options': out.options,
                        'observation': out.observation, 'keep_product_ids': out.keep_ids, 'max_item_prices': caps,
                        'exclude_product_ids': [item['product_id'] for item in items if item['group'] in replace_groups],
                        'max_total_price': round(previous[0]['total'] - .01, 2)
                            if previous and re.search(CHEAPER, text) and not cheaper_groups else None}
            try:
                result = self.registry.execute(decision.action, **args)
                if not isinstance(result, dict):
                    raise ValueError('invalid_tool_result')
            except Exception:
                result = {'ok': False, 'error': 'tool_exception', 'message': '工具暂时不可用，请稍后再试。'}
            out.executions.append((decision.action, args, result))
            out.audit.extend(['Action: ' + decision.action, 'Observation: ' + ('工具完成' if result.get('ok') else '工具失败，停止后续推荐')])
            if not result.get('ok'):
                out.failed, out.result = True, result
                return out
            if decision.action == 'inspect_outfit_photo':
                out.observation = result['data']['observation']
                photo_pending = False
                continue
            out.result = result
            return out
        out.failed = True
        out.audit.append('StepLimit: 已到达执行上限')
        out.result = {'ok': False, 'message': '本次已到达执行上限，请简短说明需要调整的衣服。'}
        return out
