"""Agent-led styling: slot dialogue, bounded ReAct, visual output, and local voice ownership."""
import ast
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, Mock

from agent.controller import AgentController
from agent.styling.engine import StylingEngine
from agent.styling.service import StylingService
from agent.styling.vision import StylingError
from test.test_styling import Catalog, Vision, photo
from test import test_styling as fixtures


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = StylingService(vision=Vision(), engine=StylingEngine(Catalog()), root=self.temp.name)
        self.patches = [patch('agent.styling.service.get_styling_service', return_value=self.service),
                        patch('agent.tools.styling_tool.get_styling_service', return_value=self.service)]
        for context in self.patches:
            context.start()
        self.controller = AgentController()

    def tearDown(self):
        for context in reversed(self.patches):
            context.stop()
        self.service.close()
        self.temp.cleanup()

    def turn(self, text, owner='alice', **kwargs):
        return self.controller.handle(text, demo_session_id=owner, **kwargs)

    def test_agent_asks_only_missing_facts_and_accepts_short_replies(self):
        first = self.turn('帮我搭配衣服')
        self.assertIn('预算', first.text)
        self.assertFalse(first.tool_calls)
        second = self.turn('上班穿')
        self.assertIn('预算', second.text)
        third = self.turn('350')
        self.assertIn('季节', third.text)
        self.assertIn('身高', third.text)
        final = self.turn('秋季，身高170cm，体重65kg，喜欢宽松，胯宽')
        self.assertEqual(final.tool_calls[-1]['tool'], 'recommend_outfit')
        options = final.presentation['plan']['options']
        self.assertEqual(options['scene'], '通勤')
        self.assertEqual(options['budget'], 350)
        self.assertEqual(options['season'], '秋季')
        self.assertEqual(options['body_type'], '胯部较宽')
        self.assertEqual(options['height_cm'], 170)
        self.assertTrue(all(look['total'] <= 350 for look in final.presentation['plan']['outfits']))
        self.assertEqual(self.service.preferences.get('alice'), {})

    def test_skip_and_direct_request_do_not_force_personal_data(self):
        self.turn('帮我搭配')
        response = self.turn('不想提供，直接推荐')
        self.assertEqual(response.tool_calls[-1]['tool'], 'recommend_outfit')
        self.assertNotIn('height_cm', response.presentation['plan']['options'])
        response = self.turn('给我夏季通勤穿搭，预算300', owner='bob')
        self.assertEqual(len(response.tool_calls), 1)
        self.assertTrue(response.presentation['plan']['outfits'])

    def test_short_currency_budget_is_not_dropped(self):
        response = self.turn('帮我通勤搭配，300元')
        self.assertEqual(response.presentation['plan']['options']['budget'], 300)

    def test_spoken_numbers_and_optional_height_redaction(self):
        from agent.fay_adapter import redact_conversation_text
        self.turn('帮我通勤搭配')
        self.turn('五百')
        self.assertIn('已脱敏', redact_conversation_text('一百七十', 'User', 'alice'))
        response = self.turn('秋天，一米七，体重一百三十斤，喜欢宽松')
        options = response.presentation['plan']['options']
        self.assertEqual(options['height_cm'], 170)
        self.assertEqual(options['weight_kg'], 65)
        self.assertEqual(options['budget'], 500)

    def test_left_page_refinement_becomes_the_next_agent_context(self):
        self.turn('通勤穿搭，预算400')
        job = self.service.latest('alice')
        self.service.refine('alice', job['job_id'], {'feedback': '日常，预算300'})
        response = self.turn('太花哨了')
        self.assertEqual(response.presentation['plan']['options']['scene'], '日常')
        self.assertEqual(response.presentation['plan']['options']['budget'], 300)

    def test_refine_preserves_photo_and_measurements_and_replaces_season(self):
        self.turn('夏季穿搭，通勤，预算400，身高170cm，体重65kg')
        response = self.turn('改成冬季，预算400')
        options = response.presentation['plan']['options']
        self.assertEqual(options['temperature'], 8)
        self.assertEqual(options['height_cm'], 170)
        self.assertTrue(any(p['group'] == 'outerwear' for p in response.presentation['plan']['outfits'][0]['items']))

    def test_staged_photo_runs_real_react_tools_and_clears_source_bytes(self):
        job = self.service.create('alice', mode='garment', stream=photo(), consent=True, defer=True)
        self.assertEqual(job['status'], 'uploaded')
        self.assertIsNotNone(self.service._jobs[job['job_id']]['image'])
        response = self.turn('夏季通勤搭配，预算300', attachment_id=job['job_id'])
        self.assertEqual([c['tool'] for c in response.tool_calls], ['inspect_outfit_photo', 'recommend_outfit'])
        self.assertEqual(response.strategy, 'react')
        self.assertTrue(any('Action: inspect_outfit_photo' == item for item in response.audit))
        self.assertTrue(any('Action: recommend_outfit' == item for item in response.audit))
        plan = response.presentation['plan']
        self.assertTrue(plan['aesthetic_advice'])
        self.assertTrue(all(p['group'] != 'tops' for look in plan['outfits'] for p in look['items']))
        self.assertIsNone(self.service._jobs[job['job_id']]['image'])
        refined = self.turn('太正式了，预算200')
        self.assertEqual(refined.presentation['plan']['observation']['mode'], 'garment')

    def test_vision_failure_or_foreign_attachment_never_queries_products(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        foreign = self.turn('通勤搭配，预算300', owner='bob', attachment_id=job['job_id'])
        self.assertEqual(len(foreign.tool_calls), 1)
        self.assertFalse(foreign.tool_calls[0]['ok'])
        with patch.object(self.service.vision, 'analyze', side_effect=StylingError('vision_timeout', '看图超时')):
            failed = self.turn('通勤搭配，预算300', attachment_id=job['job_id'])
        self.assertEqual(len(failed.tool_calls), 1)
        self.assertIn('超时', failed.text)
        self.assertIsNone(self.service._jobs[job['job_id']]['image'])

    def test_photo_opinion_is_not_an_unrequested_shopping_plan(self):
        job = self.service.create('alice', mode='person', stream=photo(), consent=True, defer=True)
        response = self.turn('只评价这身好看吗，不推荐商品', attachment_id=job['job_id'])
        self.assertEqual(len(response.tool_calls), 1)
        self.assertIn('配色', response.text)
        self.assertNotIn('plan', response.presentation)
        self.assertIsNone(self.service.latest('alice'))

    def test_product_tool_failure_returns_no_invalid_visual_plan(self):
        with patch.object(self.service.engine, 'plan', side_effect=RuntimeError('catalog offline')):
            response = self.turn('夏季通勤穿搭，预算300')
        self.assertFalse(response.tool_calls[-1]['ok'])
        self.assertIsNone(response.presentation['plan'])
        self.assertEqual(response.presentation['phase'], 'failed')

    def test_topic_switch_and_expiry_do_not_hijack_other_dialogues(self):
        self.turn('帮我搭配')
        self.assertEqual(self.turn('订单到哪里了').intent, 'logistics_query')
        self.assertFalse(self.turn('今天天气怎么样').handled)
        self.assertFalse(self.service.conversation('alice')['active'])
        self.assertFalse(self.turn('你好', owner='bob').handled)
        now = self.service.clock()
        with patch.object(self.service, 'clock', return_value=now + 1801):
            self.assertIsNone(self.service.conversation('alice'))

    def test_orders_do_not_depend_on_the_styling_runtime(self):
        with patch('agent.styling.service.get_styling_service', side_effect=RuntimeError('vision configuration error')) as service:
            response = self.turn('查询订单')
        self.assertEqual(response.intent, 'order_query')
        service.assert_not_called()

    def test_text_to_voice_continuity_and_conflicting_browser(self):
        from agent.fay_adapter import handle_text
        self.turn('帮我搭配')
        self.service.bind_voice('alice', 'User')
        with patch('agent.fay_adapter.get_controller', return_value=self.controller):
            response = handle_text('通勤，预算300', voice=True)
        self.assertIn('季节', response.text)
        self.assertEqual(self.service.local_voice_context(), ('User', 'alice'))
        with self.assertRaises(StylingError):
            self.service.bind_voice('bob', 'Other')
        self.service.bind_voice('bob', 'User', enabled=False)
        self.assertEqual(self.service.voice_owner('User'), 'alice')
        now = self.service.clock()
        with patch.object(self.service, 'clock', return_value=now + 91):
            self.assertIsNone(self.service.voice_owner('User'))


class ConversationAPItests(fixtures.APITests):
    def test_staged_chat_attachment_owner_consent_and_cleanup(self):
        missing = self.client.post('/api/styling/attachments', data={'photo': (photo(), 'photo.jpg')}, headers=self.headers)
        self.assertEqual(missing.status_code, 400)
        response = self.client.post('/api/styling/attachments', data={'photo': (photo(), 'photo.jpg'), 'mode': 'garment', 'consent': 'true'}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        key = response.json['data']['job_id']
        self.assertEqual(response.json['data']['status'], 'uploaded')
        other = self.app.test_client()
        other.get('/api/store/session')
        self.assertEqual(other.get('/api/styling/jobs/' + key).status_code, 404)
        self.assertEqual(self.client.delete('/api/styling/jobs/' + key, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/api/styling/conversation').headers['Cache-Control'], 'no-store')

    def test_local_voice_binding_rejects_remote_requests_and_requires_csrf(self):
        self.assertEqual(self.client.post('/api/styling/voice', json={'username': 'User', 'enabled': True}).status_code, 403)
        remote = self.client.post('/api/styling/voice', json={'username': 'User', 'enabled': True}, headers=self.headers,
                                  environ_overrides={'REMOTE_ADDR': '192.168.1.5'})
        self.assertEqual(remote.status_code, 403)
        local = self.client.post('/api/styling/voice', json={'username': 'User', 'enabled': True}, headers=self.headers)
        self.assertEqual(local.status_code, 200)


class FayBridgeTests(unittest.TestCase):
    def test_mic_uses_selected_browser_customer_and_owner(self):
        # Execute the real microphone callback without loading microphones or TTS during tests.
        tree = ast.parse(Path('fay_booter.py').read_text(encoding='utf-8-sig'))
        listener = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RecorderListener')
        method = next(node for node in listener.body if isinstance(node, ast.FunctionDef) and node.name == 'on_speaking')
        namespace = {'Interact': lambda kind, index, data: SimpleNamespace(data=data), 'util': Mock(), 'time': Mock(), 'feiFei': Mock()}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'fay_booter.py', 'exec'), namespace)
        service = Mock()
        service.local_voice_context.return_value = ('SelectedCustomer', 'owner-one')
        service.conversation.return_value = None
        with patch('agent.styling.service.get_styling_service', return_value=service):
            namespace['on_speaking'](object(), '秋季宽松')
        data = namespace['feiFei'].on_interact.call_args.args[0].data
        self.assertEqual(data['user'], 'SelectedCustomer')
        self.assertEqual(data['demo_session_id'], 'owner-one')

    def test_real_fay_dispatches_agent_output_to_existing_speech_stream(self):
        from agent.controller import AgentResponse
        from core.interact import Interact
        tree = ast.parse(Path('core/fay_core.py').read_text(encoding='utf-8-sig'))
        source_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'FeiFei')
        method = next(node for node in source_class.body if isinstance(node, ast.FunctionDef) and node.name == '__process_interact')
        namespace = {'Interact': Interact, 'member_db': Mock(), 'content_db': Mock(), 'util': Mock(),
                     'wsa_server': Mock(), 'cfg': Mock(), 'stream_manager': Mock()}
        namespace['content_db'].new_instance.return_value.add_content.return_value = (7, 10)
        namespace['stream_manager'].new_instance.return_value.should_stop_generation.return_value = False
        code = ast.ClassDef(name='FeiFei', bases=[], keywords=[], body=[method], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[code], type_ignores=[])), 'fay_core.py', 'exec'), namespace)
        instance = namespace['FeiFei']()
        instance._FeiFei__running = True
        instance.write_to_file = Mock()
        instance._FeiFei__process_stream_output = Mock()
        response = AgentResponse(handled=True, text='按你的预算选了两套搭配。', intent='styling', strategy='react',
                                 presentation={'revision': 'one', 'conversation_id': 'conv-one'})
        with patch('agent.fay_adapter.handle_text', return_value=response) as adapter:
            result = instance._FeiFei__process_interact(Interact('mic', 1,
                {'user': 'User', 'msg': '看看照片穿搭', 'demo_session_id': 'alice', 'conversation_id': 'conv-one'}))
        self.assertEqual(result, response.text)
        self.assertTrue(adapter.call_args.kwargs['voice'])
        instance._FeiFei__process_stream_output.assert_called_once_with(response.text, 'User', session_type='ecommerce_agent_styling', is_qa=False)
        messages = namespace['wsa_server'].get_web_instance.return_value.add_cmd.call_args_list
        self.assertTrue(any('agentPresentation' in call.args[0] for call in messages))

        # An interrupted, slow image task must not speak under the new conversation ID.
        namespace['stream_manager'].new_instance.return_value.should_stop_generation.return_value = True
        instance._FeiFei__process_stream_output.reset_mock()
        with patch('agent.fay_adapter.handle_text', return_value=response):
            self.assertEqual(instance._FeiFei__process_interact(Interact('text', 1,
                {'user': 'User', 'msg': '看照片', 'demo_session_id': 'alice', 'conversation_id': 'old-conv'})), '')
        instance._FeiFei__process_stream_output.assert_not_called()


if __name__ == '__main__':
    unittest.main()
