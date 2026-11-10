"""Repeatable dialogue/permission evaluation, NOT an image-quality benchmark.

Run with --mode model to test the configured real text provider. The catalog
and vision fixtures stay fixed in both modes; no real photos or orders are used.
"""
import argparse
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.controller import AgentController
from agent.styling.engine import StylingEngine
from agent.styling.planner import ConfiguredStylingPlanner
from agent.styling.service import StylingService
from agent.styling.vision import StylingError
from test.test_styling import Catalog, Vision, photo

CASES = ROOT / 'test' / 'fixtures' / 'styling_agent_cases.json'


def verify(response, expected, previous, service, owner):
    failures = []
    def check(condition, label):
        if not condition:
            failures.append(label)
    plan = response.presentation.get('plan')
    tools = [item['tool'] for item in response.tool_calls]
    for key in ('intent', 'handled'):
        if key in expected:
            check(getattr(response, key) == expected[key], key)
    if 'phase' in expected:
        check(response.presentation.get('phase') == expected['phase'], 'phase')
    if 'tools' in expected:
        check(tools == expected['tools'], 'tool_selection')
    if 'contains' in expected:
        check(expected['contains'] in response.text, 'answer')
    if expected.get('no_plan'):
        check(not plan, 'unexpected_plan')
    if expected.get('released'):
        check(not service.conversation(owner).get('keep_product_ids'), 'release')
    if plan:
        for key, value in expected.get('options', {}).items():
            check(plan['options'].get(key) == value, 'option:' + key)
        for key in expected.get('absent_options', []):
            check(key not in plan['options'], 'unrequested_profile:' + key)
        if 'empty' in expected:
            check(bool(plan['outfits']) != expected['empty'], 'candidate_availability')
        old_items = {i['group']: i for i in previous['outfits'][0]['items']} if previous and previous['outfits'] else {}
        for outfit in plan['outfits']:
            check(outfit['total'] <= plan['options']['budget'], 'budget')
            check(abs(sum(i['price'] for i in outfit['items']) - outfit['total']) < .001, 'price_grounding')
            check(all(i['product_id'] in {p['product_id'] for p in service.engine.catalog()} for i in outfit['items']), 'product_grounding')
            groups = {i['group'] for i in outfit['items']}
            check(set(expected.get('groups', [])) <= groups, 'missing_category')
            check(not set(expected.get('no_groups', [])) & groups, 'unwanted_category')
            for group in expected.get('keep_groups', []):
                check(group in old_items and any(i['product_id'] == old_items[group]['product_id'] for i in outfit['items']), 'keep:' + group)
            for group in expected.get('cheaper_groups', []):
                check(group in old_items and any(i['group'] == group and i['price'] < old_items[group]['price'] for i in outfit['items']), 'cheaper:' + group)
            if expected.get('cheaper_total'):
                check(previous and previous['outfits'] and outfit['total'] < previous['outfits'][0]['total'], 'cheaper_total')
        if expected.get('keep_groups') or expected.get('cheaper_groups'):
            check(bool(plan['outfits']), 'edit_has_result')
    elif any(key in expected for key in ('options', 'empty', 'groups', 'keep_groups', 'cheaper_groups')):
        failures.append('missing_visual_plan')
    return failures


def evaluate(*, mode='rule', limit=None, case_ids=None):
    cases = json.loads(CASES.read_text(encoding='utf-8'))
    if case_ids is not None:
        unknown = set(case_ids) - {case['id'] for case in cases}
        if unknown:
            raise ValueError('Unknown case IDs: ' + ', '.join(sorted(unknown)))
        cases = [case for case in cases if case['id'] in case_ids]
    cases = cases[:limit]
    results, model_turns, fallback_turns = [], 0, 0
    with tempfile.TemporaryDirectory() as runtime:
        service = StylingService(vision=Vision(), engine=StylingEngine(Catalog()), root=runtime)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch('agent.styling.service.get_styling_service', return_value=service))
                stack.enter_context(patch('agent.tools.styling_tool.get_styling_service', return_value=service))
                for case in cases:
                    controller = AgentController(styling_planner=ConfiguredStylingPlanner() if mode == 'model' else None)
                    owner, previous, failures = 'eval-' + case['id'], None, []
                    case_model_turns, case_fallbacks = 0, []
                    for index, turn in enumerate(case['turns']):
                        kwargs = {}
                        if turn.get('photo'):
                            photo_owner = 'foreign' if turn['photo'] == 'foreign' else owner
                            job = service.create(photo_owner, mode='garment' if turn['photo'] == 'garment' else 'person',
                                                 stream=photo(), consent=True, defer=True)
                            kwargs['attachment_id'] = job['job_id']
                        with ExitStack() as turn_stack:
                            if turn.get('vision_error'):
                                turn_stack.enter_context(patch.object(service.vision, 'analyze', side_effect=StylingError('vision_timeout', '看图超时')))
                            response = controller.handle(turn['text'], demo_session_id=owner, **kwargs)
                        model_turns += any(line.startswith('ModelDecision:') for line in response.audit)
                        fallback_turns += any(line.startswith('PlannerFallback:') for line in response.audit)
                        case_model_turns += any(line.startswith('ModelDecision:') for line in response.audit)
                        case_fallbacks.extend(line.split(';', 1)[0].split('；', 1)[0] for line in response.audit
                                              if line.startswith('PlannerFallback:'))
                        failures.extend(f'turn {index + 1}: {failure}' for failure in verify(response, turn.get('expect', {}), previous, service, owner))
                        previous = response.presentation.get('plan') or previous
                    results.append({'id': case['id'], 'name': case['name'], 'passed': not failures,
                                    'model_turns': case_model_turns, 'fallbacks': case_fallbacks, 'failures': failures})
                    # Separate dialogue cases must not share completed or staged photos.
                    for key, job in list(service._jobs.items()):
                        service.delete(job['owner'], key)
        finally:
            service.close()
    return {'mode': mode, 'catalog': 'fixed_fixture', 'vision': 'fixed_fixture', 'cases': len(results),
            'passed': sum(r['passed'] for r in results), 'model_turns': model_turns,
            'fallback_turns': fallback_turns, 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['rule', 'model'], default='rule')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--cases', help='Comma-separated case IDs, e.g. 14,24,31')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    try:
        result = evaluate(mode=args.mode, limit=args.limit,
                          case_ids={value.strip() for value in args.cases.split(',')} if args.cases else None)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] == result['cases'] and (args.mode != 'model' or result['model_turns']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
