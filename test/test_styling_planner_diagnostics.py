"""User-facing fallback reasons remain accurate without leaking provider data."""
import unittest
from unittest.mock import Mock, patch

import requests

from agent.styling import planner
from test import test_styling_conversation as conversation_fixtures
from test.test_styling_planner import SequencePlanner


class PlannerFallbackNoticeTests(unittest.TestCase):
    def test_service_failures_have_distinct_notices(self):
        cases = {
            'provider_timeout': '超时',
            'provider_rate_limited': '繁忙',
            'provider_auth_error': '配置',
            'provider_unconfigured': '配置',
            'provider_http_error': '连接',
            'provider_connection_error': '连接',
        }
        for code, explanation in cases.items():
            with self.subTest(code=code):
                notice = planner.fallback_notice(code)
                self.assertIn(explanation, notice)
                self.assertIn('按已确认条件', notice)

    def test_rejected_decisions_are_not_misreported_as_service_outages(self):
        for code in ('invalid_json', 'invalid_action', 'ungrounded_update',
                     'invented_budget', 'repeated_or_invalid_question'):
            with self.subTest(code=code):
                notice = planner.fallback_notice(code)
                self.assertIn('未通过校验', notice)
                self.assertIn('按已确认条件', notice)
                self.assertNotIn('服务暂不可用', notice)

    def test_unknown_reason_cannot_echo_arbitrary_sensitive_text(self):
        notice = planner.fallback_notice('Authorization: private-token customer-photo.jpg')
        self.assertIn('本轮未完成模型决策', notice)
        self.assertIn('按已确认条件', notice)
        for private in ('Authorization', 'private-token', 'customer-photo.jpg'):
            self.assertNotIn(private, notice)

    def test_connection_failures_and_builtin_timeouts_have_safe_reason_codes(self):
        self.assertEqual(planner.failure_code(requests.ConnectionError('private-key')),
                         'provider_connection_error')
        self.assertEqual(planner.failure_code(TimeoutError('private-key')),
                         'provider_timeout')

    def test_invalid_preference_values_are_reported_as_decision_validation_failures(self):
        for key, value in (('budget', True), ('scene', 'private-invalid-scene')):
            with self.subTest(field=key):
                decision = {'action': 'recommend_outfit', 'updates': {key: value},
                            'evidence': {key: '本轮原话'}}
                with self.assertRaises(planner.PlannerError) as caught:
                    planner.parse_decision(decision, '本轮原话')
                self.assertEqual(str(caught.exception), 'invalid_updates')
                self.assertNotIn('private-invalid-scene', str(caught.exception))

    def test_malformed_provider_responses_have_one_safe_protocol_error(self):
        from utils import config_util as cfg
        cases = [
            ValueError('private malformed JSON body'),
            {}, {'choices': []}, {'choices': None},
            {'choices': [{'message': {}}]},
            {'choices': [{'message': {'content': None}}]},
            {'choices': [{'message': {'content': ['private-content']}}]},
            {'choices': [{'message': {'content': {'private': 'content'}}}]},
        ]
        with patch.multiple(cfg, system_config=object(),
                            gpt_base_url='http://127.0.0.1:9999/v1',
                            gpt_model_engine='test-model', key_gpt_api_key='test-secret'):
            for index, value in enumerate(cases):
                with self.subTest(case=index):
                    response = Mock()
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    if isinstance(value, Exception):
                        response.json.side_effect = value
                    else:
                        response.json.return_value = value
                    with patch('requests.post', return_value=response):
                        with self.assertRaises(planner.PlannerError) as caught:
                            planner.ConfiguredStylingPlanner().decide({'text': '通勤穿搭'})
                    self.assertEqual(str(caught.exception), 'invalid_provider_content')
                    self.assertNotIn('private', str(caught.exception))


class PlannerFallbackConversationTests(unittest.TestCase):
    setUp = conversation_fixtures.DialogueTests.setUp
    tearDown = conversation_fixtures.DialogueTests.tearDown
    turn = conversation_fixtures.DialogueTests.turn

    def test_controller_notices_match_the_actual_fallback_and_keep_tools_working(self):
        cases = [
            (requests.Timeout('secret timeout response'), 'provider_timeout'),
            (requests.ConnectionError('secret connection response'), 'provider_connection_error'),
            (planner.PlannerError('invalid_json'), 'invalid_json'),
        ]
        for index, (error, code) in enumerate(cases):
            with self.subTest(code=code):
                self.controller.styling_planner = SequencePlanner(error)
                response = self.turn('夏季通勤穿搭，预算300', owner='diagnostic-' + str(index))
                self.assertEqual(response.presentation['fallback_reason'], code)
                self.assertEqual(response.presentation['planner_notice'], planner.fallback_notice(code))
                self.assertTrue(response.presentation['planner_fallback'])
                self.assertEqual(response.presentation['decision_mode'], 'rule')
                self.assertEqual([call['tool'] for call in response.tool_calls], ['recommend_outfit'])
                self.assertTrue(response.presentation['plan']['outfits'])
                self.assertTrue(all(outfit['total'] <= 300
                                    for outfit in response.presentation['plan']['outfits']))
                self.assertNotIn('secret', str(response))

    def test_fallback_log_explains_the_failure_without_user_or_provider_details(self):
        self.controller.styling_planner = SequencePlanner(
            requests.Timeout('Authorization: secret-provider-response'))
        with self.assertLogs('agent.styling.workflow', level='WARNING') as captured:
            self.turn('夏季通勤穿搭，预算300', owner='private-owner-token')
        output = '\n'.join(captured.output)
        self.assertIn('provider_timeout', output)
        self.assertIn('action=', output)
        self.assertIn('elapsed_ms=', output)
        for private in ('Authorization', 'secret-provider-response', 'private-owner-token',
                        '夏季通勤穿搭', '预算300'):
            self.assertNotIn(private, output)


if __name__ == '__main__':
    unittest.main()
