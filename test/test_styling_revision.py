"""Conversation regressions for the reported color/budget failure and safe diagnostics."""
import unittest

import requests

from agent.styling.planner import failure_code
from test import test_styling_conversation as conversation_fixtures
from test.test_styling_planner import SequencePlanner


QUESTION = '给我再挑一套不以黑色为裤子，不以白色为外套，不以红色为内搭的穿搭，价格不限'


class StylingRevisionTests(unittest.TestCase):
    setUp = conversation_fixtures.DialogueTests.setUp
    tearDown = conversation_fixtures.DialogueTests.tearDown
    turn = conversation_fixtures.DialogueTests.turn

    def seed(self):
        self.service.update_conversation('alice', options={'scene': '通勤', 'season': '秋季', 'budget': 300},
                                         waiting=[], personal_asked=True, active=True)

    def assert_constraints(self, response):
        plan = response.presentation['plan']
        self.assertTrue(plan['options']['budget_unlimited'])
        self.assertNotIn('budget', plan['options'])
        self.assertTrue(plan['outfits'])
        for look in plan['outfits']:
            self.assertEqual({item['group'] for item in look['items']}, {'tops', 'bottoms', 'outerwear'})
            for item in look['items']:
                self.assertFalse(set(item['colors']) & set(
                    plan['options']['excluded_colors_by_group'].get(item['group'], [])))
        self.assertIn('价格不限', response.text)
        self.assertIn('内搭避开红色', response.text)
        self.assertNotIn('300 预算内', response.text)

    def test_rule_fallback_honors_original_request(self):
        self.seed()
        self.controller.styling_planner = SequencePlanner(requests.Timeout('private provider text'))
        response = self.turn(QUESTION)
        self.assert_constraints(response)
        self.assertTrue(response.presentation['planner_fallback'])
        self.assertEqual(response.presentation['fallback_reason'], 'provider_timeout')
        self.assertIn('按已确认条件', response.presentation['planner_notice'])
        self.assertIn('超时', response.presentation['planner_notice'])
        self.assertNotIn('private provider text', str(response))

    def test_model_receives_unlimited_budget_and_calls_real_tool(self):
        self.seed()
        planner = SequencePlanner({'action': 'recommend_outfit'})
        self.controller.styling_planner = planner
        response = self.turn(QUESTION)
        self.assert_constraints(response)
        self.assertEqual(response.presentation['decision_mode'], 'model')
        self.assertNotIn('budget', planner.contexts[0]['missing_fields'])
        self.assertTrue(planner.contexts[0]['options']['budget_unlimited'])
        self.assertFalse(response.presentation['planner_fallback'])
        self.assertEqual(response.presentation['workflow_nodes'], ['decide', 'execute_tool', 'finalize'])

    def test_model_cannot_drop_explicit_color_exclusions(self):
        self.seed()
        self.controller.styling_planner = SequencePlanner({
            'action': 'recommend_outfit', 'updates': {'excluded_colors_by_group': {}},
            'evidence': {'excluded_colors_by_group': '不以黑色为裤子'}})
        self.assert_constraints(self.turn(QUESTION))

    def test_model_cannot_invent_unlimited_budget(self):
        self.controller.styling_planner = SequencePlanner({
            'action': 'recommend_outfit', 'updates': {'budget_unlimited': True},
            'evidence': {'budget_unlimited': '通勤'}})
        response = self.turn('秋季通勤搭配，预算300')
        self.assertEqual(response.presentation['fallback_reason'], 'invented_budget')
        self.assertEqual(response.presentation['plan']['options']['budget'], 300)

    def test_model_delta_keeps_previous_groups_and_excluded_colors(self):
        self.seed()
        self.turn(QUESTION)
        self.controller.styling_planner = SequencePlanner({
            'action': 'recommend_outfit', 'updates': {'excluded_colors_by_group': {'bottoms': ['灰']}},
            'evidence': {'excluded_colors_by_group': '裤子不要灰色'}})
        response = self.turn('裤子不要灰色')
        colors = response.presentation['plan']['options']['excluded_colors_by_group']
        self.assertEqual(set(colors['bottoms']), {'黑', '灰'})
        self.assertEqual(colors['outerwear'], ['白'])
        self.assertEqual(colors['tops'], ['红'])

    def test_fresh_unlimited_budget_never_gets_default_300_on_skip(self):
        response = self.turn('秋季通勤穿搭，价格不限，直接推荐')
        self.assertTrue(response.presentation['plan']['options']['budget_unlimited'])
        self.assertNotIn('budget', response.presentation['plan']['options'])

    def test_requesting_new_numeric_budget_after_unlimited(self):
        self.turn('秋季通勤穿搭，价格不限，直接推荐')
        response = self.turn('预算400')
        self.assertEqual(response.presentation['plan']['options']['budget'], 400)
        self.assertFalse(response.presentation['plan']['options'].get('budget_unlimited'))


class SafePlannerDiagnosticsTests(unittest.TestCase):
    def test_only_safe_status_codes_are_reported(self):
        for status, expected in ((429, 'provider_rate_limited'), (401, 'provider_auth_error'),
                                 (403, 'provider_auth_error'), (500, 'provider_http_error')):
            response = requests.Response()
            response.status_code = status
            error = requests.HTTPError('Authorization secret-value', response=response)
            self.assertEqual(failure_code(error), expected)


if __name__ == '__main__':
    unittest.main()
