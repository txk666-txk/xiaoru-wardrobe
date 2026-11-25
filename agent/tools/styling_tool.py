from __future__ import annotations

import re

from agent.styling.engine import parse_feedback
from agent.styling.service import get_styling_service
from agent.styling.vision import StylingError
from .common import tool_result


def describe_plan(plan):
    options = plan['options']
    if not plan['outfits']:
        return plan['summary']
    lines = [*plan.get('aesthetic_advice', [])[:1]]
    first = plan['outfits'][0]
    if options.get('only_pants'):
        budget = '价格不限' if options.get('budget_unlimited') else f"预算 ¥{options['budget']:.0f}"
        photo = '参考照片里的上衣，' if any(
            item.get('category') in {'tops', 'outerwear'} for item in (plan.get('observation') or {}).get('garments', [])) else ''
        return (f"{photo}给你挑了 {len(plan['outfits'])} 款裤子（{budget}）。"
                "每款价格和试穿入口都在下方的商品卡片上。")
    labels = {'tops': '内搭', 'bottoms': '裤装', 'outerwear': '外套', 'one-pieces': '裙装',
              'shoes': '鞋', 'accessory': '配饰'}
    exclusions = [labels[group] + '避开' + '、'.join(color + '色' for color in colors)
                  for group, colors in options.get('excluded_colors_by_group', {}).items() if colors]
    if options.get('excluded_colors'):
        exclusions.append('整套避开' + '、'.join(color + '色' for color in options['excluded_colors']))
    if exclusions:
        lines.append('这次按你的要求：' + '；'.join(exclusions) + '。')
    lines.extend(plan.get('constraint_notes', [])[:2])
    budget = '价格不限，不沿用之前的预算' if options.get('budget_unlimited') else f"在 ¥{options['budget']:.0f} 预算内"
    lines.append(f"按{options.get('season', '当前季节')}的{options['scene']}需求，挑了{len(plan['outfits'])}套，"
                 f"第一套新增商品参考合计 ¥{first['total']:.2f}，{budget}。")
    pieces = [labels.get(item['group'], '单品') + '：' + item['name'][:30] for item in first['items']]
    lines.append('第一套是' + '；'.join(pieces) + '。')
    lines.append('具体商品在下方，可直接试穿；颜色按目录资料筛选，请结合商品图确认。')
    return "\n".join(lines)


class StylingTool:
    name = "recommend_outfit"
    description = "按用途预算规划整套搭配，依据本会话照片观察调整、保留单品或比较商品"

    def execute(self, *, query="", demo_session_id=None, options=None, observation=None,
                keep_product_ids=None, exclude_product_ids=None, max_item_prices=None, max_total_price=None, **_):
        try:
            if options is not None:
                service = get_styling_service()
                current = service.latest(demo_session_id)
                keep = list(keep_product_ids or [])
                if keep_product_ids is None and '保留' in query and current:
                    target = 'bottoms' if '裤' in query else 'outerwear' if '外套' in query else 'tops'
                    keep = [p['product_id'] for p in current['result']['outfits'][0]['items'] if p['group'] == target] if current['result']['outfits'] else []
                plan = service.engine.plan(options, observation, keep, exclude_product_ids, max_item_prices=max_item_prices,
                                           max_total_price=max_total_price)
                service.remember_plan(demo_session_id, plan)
                return tool_result(ok=True, message=describe_plan(plan), data=plan)
            return self._execute(query=query, demo_session_id=demo_session_id)
        except StylingError as exc:
            return tool_result(ok=False, message=str(exc), error=exc.code)

    def _execute(self, *, query="", demo_session_id=None):
        service = get_styling_service()
        if any(word in query for word in ("先不买", "暂时不买", "不想买")):
            return tool_result(ok=True, message="当然可以先不买。已有衣服也能搭出新感觉；你可以上传已有单品，我帮你看配色和组合，是否添置由你决定。", data={})
        ids = list(dict.fromkeys(re.findall(r"\bYZREF-[A-Z0-9]+", query.upper())))
        if any(word in query for word in ("比较", "对比", "哪个更", "哪件更")) and len(ids) >= 2:
            comparison = service.engine.compare(ids[:3], parse_feedback(query))
            return tool_result(ok=True, message=comparison["summary"], data=comparison)
        current = service.latest(demo_session_id) if demo_session_id else None
        if current:
            updated = service.refine(demo_session_id, current["job_id"], {"feedback": query})
            return tool_result(ok=True, message=describe_plan(updated["result"]), data=updated["result"])
        preferences = service.preferences.get(demo_session_id) if demo_session_id else {}
        options = parse_feedback(query, preferences)
        if any(word in query for word in ("照片", "这张", "已有衣服")) and not current:
            return tool_result(ok=True, message="可以直接用聊天输入框旁的图片按钮上传本人照或已有衣服。我会观察配色与服装，再按用途和预算推荐。", data={})
        plan = service.engine.plan(options)
        service.remember_plan(demo_session_id, plan)
        return tool_result(ok=True, message=describe_plan(plan), data=plan)


class OutfitPhotoTool:
    name = 'inspect_outfit_photo'
    description = '观察当前会话上传的衣服或穿着照片，返回本地视觉证据与配色、轮廓建议'

    def execute(self, *, attachment_id, demo_session_id, **_):
        from agent.styling.engine import aesthetic_advice
        try:
            observation = get_styling_service().inspect_photo(demo_session_id, attachment_id)
            advice = aesthetic_advice(observation)
            return tool_result(ok=True, message='\n'.join(advice), data={'observation': observation, 'aesthetic_advice': advice})
        except StylingError as exc:
            return tool_result(ok=False, message=str(exc), error=exc.code)
