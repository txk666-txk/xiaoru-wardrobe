"""Model decisions are untrusted proposals; real tools own facts and permissions."""
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from agent.styling.planner import ConfiguredStylingPlanner, PlannerError, parse_decision, configured_planner
from test import test_styling_conversation as conversation_fixtures
from test.test_styling import photo


class SequencePlanner:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(copy.deepcopy(context))
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        return decision(context) if callable(decision) else json.dumps(decision, ensure_ascii=False)


class ModelStylingTests(unittest.TestCase):
    setUp = conversation_fixtures.DialogueTests.setUp
    tearDown = conversation_fixtures.DialogueTests.tearDown
    turn = conversation_fixtures.DialogueTests.turn

    def use(self, *decisions):
        planner = SequencePlanner(*decisions)
        self.controller.styling_planner = planner
        return planner

    def test_model_observes_real_photo_result_then_selects_recommendation(self):
        job = self.service.create('alice', mode='garment', stream=photo(), consent=True, defer=True)
        def after_observation(context):
            self.assertTrue(context['observation']['garments'])
            self.assertFalse(context['photo_pending'])
            return {'action': 'recommend_outfit'}
        planner = self.use({'action': 'inspect_outfit_photo'}, after_observation)
        response = self.turn('夏季通勤搭配，预算300', attachment_id=job['job_id'])
        self.assertEqual(response.strategy, 'model_react')
        self.assertEqual([c['tool'] for c in response.tool_calls], ['inspect_outfit_photo', 'recommend_outfit'])
        self.assertEqual(len(planner.contexts), 2)
        self.assertTrue(all(p['group'] != 'tops' for o in response.presentation['plan']['outfits'] for p in o['items']))
        serialized = json.dumps(planner.contexts)
        self.assertNotIn('alice', serialized)
        self.assertNotIn(job['job_id'], serialized)

    def test_model_understands_an_unparsed_reply_and_avoids_repeating_questions(self):
        self.turn('帮我搭配')
        self.use({'action': 'recommend_outfit', 'updates': {'scene': '通勤', 'budget': 250},
                  'evidence': {'scene': '坐班', 'budget': '最多250'}})
        response = self.turn('周一到周五坐班，花销最多250')
        self.assertEqual(response.presentation['plan']['options']['scene'], '通勤')
        self.assertEqual(response.presentation['plan']['options']['budget'], 250)
        self.assertEqual(len(response.tool_calls), 1)

    def test_missing_slot_is_asked_without_product_query(self):
        self.use({'action': 'ask', 'question_fields': ['budget']})
        response = self.turn('帮我通勤搭配')
        self.assertIn('预算', response.text)
        self.assertFalse(response.tool_calls)
        self.assertFalse(response.presentation['planner_fallback'])

    def test_invalid_or_unsafe_outputs_fall_back_without_executing_them(self):
        invalid = [
            {'action': 'refund_order'},
            {'action': 'recommend_outfit', 'demo_session_id': 'victim'},
            {'action': 'recommend_outfit', 'updates': {'height_cm': 180}, 'evidence': {'height_cm': '通勤'}},
            {'action': 'recommend_outfit', 'updates': {'budget': 900}, 'evidence': {'budget': '预算300'}},
            {'action': 'ask', 'question_fields': ['budget']},
            {'action': 'recommend_outfit', 'updates': {'gender': '女款'}},
            {'action': 'recommend_outfit', 'keep_groups': ['bottoms']},
            {'action': 'recommend_outfit', 'updates': {'budget': True}, 'evidence': {'budget': '预算300'}},
            {'action': 'recommend_outfit', 'question_fields': [42]},
        ]
        for index, decision in enumerate(invalid):
            with self.subTest(decision=decision):
                self.use(decision)
                response = self.turn('夏季通勤穿搭，预算300', owner='case' + str(index))
                self.assertEqual([c['tool'] for c in response.tool_calls], ['recommend_outfit'])
                self.assertTrue(response.presentation['planner_fallback'])
                self.assertEqual(response.presentation['plan']['options']['budget'], 300)

    def test_timeout_does_not_expose_provider_details(self):
        self.use(TimeoutError('Authorization: secret-value'))
        response = self.turn('夏季通勤穿搭，预算300')
        self.assertEqual(response.presentation['decision_mode'], 'rule')
        self.assertNotIn('secret-value', str(response))
        self.assertTrue(response.presentation['plan']['outfits'])

    def test_failed_second_decision_does_not_repeat_vision(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        planner = self.use({'action': 'inspect_outfit_photo'}, {'action': 'inspect_outfit_photo'})
        response = self.turn('夏季通勤搭配，预算300', attachment_id=job['job_id'])
        self.assertEqual(len(response.tool_calls), 2)
        self.assertEqual(len(planner.contexts), 2)
        self.assertTrue(response.presentation['planner_fallback'])
        self.assertIsNone(self.service._jobs[job['job_id']]['image'])

    def test_foreign_photo_stops_before_any_other_tool_or_decision(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        planner = self.use({'action': 'inspect_outfit_photo'})
        response = self.turn('通勤搭配，预算300', owner='bob', attachment_id=job['job_id'])
        self.assertFalse(response.tool_calls[0]['ok'])
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(len(response.tool_calls), 1)

    def test_model_cannot_skip_attachment_verification(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        self.use({'action': 'recommend_outfit'})
        response = self.turn('通勤搭配，预算300', owner='bob', attachment_id=job['job_id'])
        self.assertEqual(response.tool_calls[0]['tool'], 'inspect_outfit_photo')
        self.assertFalse(response.tool_calls[0]['ok'])

    def test_opinion_only_never_sells_even_when_model_asks_to(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        self.use({'action': 'inspect_outfit_photo'}, {'action': 'recommend_outfit'})
        response = self.turn('只评价这身好看吗，不推荐商品', attachment_id=job['job_id'])
        self.assertEqual(len(response.tool_calls), 1)
        self.assertNotIn('plan', response.presentation)

    def test_model_can_recognize_opinion_without_exact_rule_keywords(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        self.use({'action': 'inspect_outfit_photo'}, {'action': 'opinion'})
        response = self.turn('你觉得这身颜色协调不协调', attachment_id=job['job_id'])
        self.assertEqual(len(response.tool_calls), 1)
        self.assertTrue(response.presentation['aesthetic_advice'])
        self.assertFalse(response.presentation['planner_fallback'])

    def test_request_for_cheaper_item_releases_its_previous_retention(self):
        self.turn('夏季通勤穿搭，预算300')
        retained = self.turn('保留上衣')
        old = {item['group']: item for item in retained.presentation['plan']['outfits'][0]['items']}
        self.use({'action': 'recommend_outfit'})
        response = self.turn('上衣便宜一点')
        new = {item['group']: item for item in response.presentation['plan']['outfits'][0]['items']}
        self.assertLess(new['tops']['price'], old['tops']['price'])
        self.assertEqual(new['bottoms']['product_id'], old['bottoms']['product_id'])

    def test_replacing_a_single_group_really_changes_it_and_preserves_the_rest(self):
        initial = self.turn('夏季通勤穿搭，预算300')
        old = {item['group']: item for item in initial.presentation['plan']['outfits'][0]['items']}
        self.use({'action': 'recommend_outfit'})
        response = self.turn('只换一件上衣')
        new = {item['group']: item for item in response.presentation['plan']['outfits'][0]['items']}
        self.assertNotEqual(new['tops']['product_id'], old['tops']['product_id'])
        self.assertEqual(new['bottoms']['product_id'], old['bottoms']['product_id'])

    def test_model_can_propose_a_grounded_replacement_for_semantic_followup(self):
        initial = self.turn('夏季通勤穿搭，预算300')
        old = {item['group']: item for item in initial.presentation['plan']['outfits'][0]['items']}
        self.use({'action': 'recommend_outfit', 'replace_groups': ['tops'],
                  'group_evidence': {'tops': '上半身想试个别的'}})
        response = self.turn('上半身想试个别的')
        new = {item['group']: item for item in response.presentation['plan']['outfits'][0]['items']}
        self.assertNotEqual(new['tops']['product_id'], old['tops']['product_id'])
        self.assertEqual(new['bottoms']['product_id'], old['bottoms']['product_id'])

    def test_general_cheaper_does_not_invent_a_twenty_percent_budget_reduction(self):
        initial = self.turn('夏季通勤穿搭，预算200')
        response = self.turn('便宜一点')
        plan = response.presentation['plan']
        self.assertEqual(plan['options']['budget'], 200)
        self.assertTrue(plan['outfits'])
        self.assertLess(plan['outfits'][0]['total'], initial.presentation['plan']['outfits'][0]['total'])
        self.service.remember_plan('legacy', initial.presentation['plan'])
        current = self.service.latest('legacy')
        updated = self.service.refine('legacy', current['job_id'], {'feedback': '便宜一点'})['result']
        self.assertEqual(updated['options']['budget'], 200)
        self.assertLess(updated['outfits'][0]['total'], initial.presentation['plan']['outfits'][0]['total'])

    def test_model_budget_never_overrides_explicit_parsed_ceiling(self):
        self.use({'action': 'recommend_outfit', 'updates': {'budget': 500}, 'evidence': {'budget': '原来500'}})
        response = self.turn('通勤搭配，原来500，现在预算200')
        self.assertEqual(response.presentation['plan']['options']['budget'], 200)
        self.assertTrue(all(o['total'] <= 200 for o in response.presentation['plan']['outfits']))

    def test_scoped_cheaper_replacement_really_costs_less_and_keeps_pants(self):
        initial = self.turn('夏季通勤搭配，预算300')
        items = initial.presentation['plan']['outfits'][0]['items']
        old = {item['group']: item for item in items}
        self.use({'action': 'recommend_outfit'})
        response = self.turn('裤子保留，上衣便宜一点')
        new = {item['group']: item for item in response.presentation['plan']['outfits'][0]['items']}
        self.assertEqual(new['bottoms']['product_id'], old['bottoms']['product_id'])
        self.assertLess(new['tops']['price'], old['tops']['price'])
        self.assertEqual(response.presentation['plan']['options']['budget'], 300)
        self.use({'action': 'recommend_outfit'})
        next_turn = self.turn('太花哨了')
        self.assertTrue(all(any(item['product_id'] == old['bottoms']['product_id'] for item in outfit['items'])
                            for outfit in next_turn.presentation['plan']['outfits']))

    def test_model_can_understand_other_words_for_scoped_edit(self):
        self.turn('夏季通勤搭配，预算300')
        self.use({'action': 'recommend_outfit', 'keep_groups': ['bottoms'], 'cheaper_groups': ['tops'],
                  'group_evidence': {'bottoms': '下边还是原来那件', 'tops': '上边省一点'}})
        response = self.turn('下边还是原来那件，上边省一点')
        self.assertEqual(response.presentation['plan']['outfits'][0]['total'], 180)

    def test_release_previously_kept_group(self):
        self.turn('夏季通勤搭配，预算300')
        self.turn('保留裤子')
        self.assertTrue(self.service.conversation('alice')['keep_product_ids'])
        self.use({'action': 'recommend_outfit'})
        self.turn('不用保留裤子')
        self.assertFalse(self.service.conversation('alice')['keep_product_ids'])

    def test_no_cheaper_item_is_reported_without_repeating_old_item(self):
        self.turn('夏季日常搭配，预算300，简约')
        response = self.turn('裤子不变，上衣再便宜一些')
        self.assertFalse(response.presentation['plan']['outfits'])
        self.assertIn('未找到', response.text)

    def test_skip_cannot_be_overridden_by_model_optional_question(self):
        self.turn('帮我搭配')
        self.use({'action': 'ask', 'question_fields': ['profile']})
        response = self.turn('不想提供，直接推荐')
        self.assertTrue(response.presentation['plan']['outfits'])

    def test_transaction_route_bypasses_styling_model(self):
        self.turn('帮我搭配')
        planner = self.use()
        self.assertEqual(self.turn('我的订单到哪里了').intent, 'logistics_query')
        self.assertEqual(self.turn('我要退款').intent, 'after_sales')
        self.assertFalse(planner.contexts)

    def test_unrelated_topic_returns_to_original_fay_even_if_planner_times_out(self):
        for choice in ({'action': 'handoff'}, TimeoutError()):
            self.turn('帮我搭配')
            self.use(choice)
            response = self.turn('今天天气怎么样')
            self.assertFalse(response.handled)
            self.assertFalse(response.tool_calls)
            self.assertFalse(self.service.conversation('alice')['active'])
            self.controller.styling_planner = None

    def test_context_redacts_body_values_and_preserves_them_for_local_sizing(self):
        planner = self.use({'action': 'recommend_outfit'})
        response = self.turn('秋季通勤穿搭，预算400，身高173cm，体重67kg，胯宽')
        serialized = json.dumps(planner.contexts, ensure_ascii=False)
        for value in ('173', '67', '胯宽', 'alice'):
            self.assertNotIn(value, serialized)
        self.assertEqual(response.presentation['plan']['options']['height_cm'], 173)


class DecisionProtocolTests(unittest.TestCase):
    def test_forty_dialogue_scenarios_pass_offline(self):
        from scripts.evaluate_styling_agent import evaluate
        result = evaluate()
        self.assertEqual(result['cases'], 40)
        self.assertEqual(result['passed'], 40, [r for r in result['results'] if not r['passed']])

    def test_fenced_json_and_invalid_payloads(self):
        self.assertEqual(parse_decision('```json\n{"action":"recommend_outfit"}\n```', '').action, 'recommend_outfit')
        for value in ('not JSON', '[]', '{"action":[]}', '{"action":"delete_file"}', 'x' * 12001):
            with self.subTest(value=value[:50]), self.assertRaises(PlannerError):
                parse_decision(value, '')

    def test_explicit_rule_switch(self):
        with patch.dict(os.environ, {'FAY_STYLING_PLANNER': 'rule'}):
            self.assertIsNone(configured_planner())
        with patch.dict(os.environ, {'FAY_STYLING_PLANNER': 'model'}):
            self.assertIsInstance(configured_planner(), ConfiguredStylingPlanner)

    def test_configured_provider_reuses_existing_settings_with_bounded_timeout(self):
        from utils import config_util as cfg
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = {'choices': [{'message': {'content': '{"action":"recommend_outfit"}'}}]}
        with patch.multiple(cfg, system_config=object(), gpt_base_url='http://127.0.0.1:9999/v1',
                            gpt_model_engine='test-model', key_gpt_api_key='test-secret'):
            with patch('requests.post', return_value=response) as post:
                result = ConfiguredStylingPlanner().decide({'text': '通勤穿搭'})
        self.assertEqual(parse_decision(result, '').action, 'recommend_outfit')
        self.assertEqual(post.call_args.args[0], 'http://127.0.0.1:9999/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['timeout'], (3, 20))
        self.assertEqual(post.call_args.kwargs['json']['model'], 'test-model')
        self.assertNotIn('thinking', post.call_args.kwargs['json'])
        self.assertNotIn('test-secret', json.dumps(post.call_args.kwargs['json']))

    def test_thinking_switch_is_scoped_to_supported_bigmodel_models(self):
        from utils import config_util as cfg
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = {'choices': [{'message': {'content': '{"action":"ask"}'}}]}
        for base, model, disabled in (
                ('https://open.bigmodel.cn/api/paas/v4', 'glm-5.1', True),
                ('https://open.bigmodel.cn/api/paas/v4', 'glm-5.3', False),
                ('http://127.0.0.1:9999/v1', 'glm-5.1', False)):
            with self.subTest(base=base, model=model), patch.multiple(cfg, system_config=object(),
                    gpt_base_url=base, gpt_model_engine=model, key_gpt_api_key='test-secret'):
                with patch('requests.post', return_value=response) as post:
                    ConfiguredStylingPlanner().decide({'text': '帮我穿搭'})
                self.assertEqual(post.call_args.kwargs['json'].get('thinking'),
                                 {'type': 'disabled'} if disabled else None)


if __name__ == '__main__':
    unittest.main()
