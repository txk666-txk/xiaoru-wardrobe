"""Offline integration checks for real LangGraph styling orchestration."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import os
import threading
import unittest
from unittest.mock import Mock, patch

import langsmith
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.runnables import RunnableLambda
from langchain_core.tracers.context import tracing_v2_callback_var, tracing_v2_enabled
from langchain_core.tracers.langchain import LangChainTracer
from langgraph.graph.state import CompiledStateGraph

from agent.styling.vision import StylingError
from agent.styling.workflow import StylingWorkflow
from test import test_styling_conversation as conversation_fixtures
from test.test_styling import photo
from test.test_styling_planner import SequencePlanner


class RecordingCallback(BaseCallbackHandler):
    """Capture actual callback payloads without any network or logging."""
    run_inline = True

    def __init__(self):
        super().__init__()
        self.inputs = []
        self.outputs = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.inputs.append(copy.deepcopy(inputs))

    def on_chain_end(self, outputs, **kwargs):
        self.outputs.append(copy.deepcopy(outputs))


class StylingGraphConversationTests(unittest.TestCase):
    setUp = conversation_fixtures.DialogueTests.setUp
    tearDown = conversation_fixtures.DialogueTests.tearDown
    turn = conversation_fixtures.DialogueTests.turn

    def assert_graph(self, response, expected_nodes):
        self.assertEqual(response.presentation['workflow_engine'], 'langgraph')
        self.assertEqual(response.presentation['workflow_nodes'], expected_nodes)

    def test_controller_really_invokes_compiled_graph(self):
        original_invoke = CompiledStateGraph.invoke
        with patch.object(CompiledStateGraph, 'invoke', autospec=True,
                          side_effect=original_invoke) as invoke:
            response = self.turn('夏季通勤穿搭，预算300')
        self.assertEqual(invoke.call_count, 1)
        self.assertIsInstance(invoke.call_args.args[0], CompiledStateGraph)
        self.assert_graph(response, ['decide', 'execute_tool', 'finalize'])
        self.assertTrue(response.presentation['plan']['outfits'])

    def test_photo_observation_returns_to_model_before_recommendation(self):
        staged = self.service.create('alice', mode='garment', stream=photo(),
                                     consent=True, defer=True)

        def after_observation(context):
            self.assertTrue(context['observation']['garments'])
            self.assertFalse(context['photo_pending'])
            return {'action': 'recommend_outfit'}

        planner = SequencePlanner({'action': 'inspect_outfit_photo'}, after_observation)
        self.controller.styling_planner = planner
        response = self.turn('夏季通勤搭配，预算300', attachment_id=staged['job_id'])
        self.assert_graph(response, ['decide', 'execute_tool', 'decide', 'execute_tool', 'finalize'])
        self.assertEqual(response.strategy, 'model_react')
        self.assertEqual([call['tool'] for call in response.tool_calls],
                         ['inspect_outfit_photo', 'recommend_outfit'])
        self.assertEqual(len(planner.contexts), 2)
        self.assertIsNone(self.service._jobs[staged['job_id']]['image'])

    def test_vision_failure_ends_graph_without_retrying_or_recommending(self):
        staged = self.service.create('alice', mode='person', stream=photo(),
                                     consent=True, defer=True)
        planner = SequencePlanner({'action': 'inspect_outfit_photo'})
        self.controller.styling_planner = planner
        with patch.object(self.service.vision, 'analyze',
                          side_effect=StylingError('vision_timeout', '看图超时')):
            response = self.turn('夏季通勤搭配，预算300', attachment_id=staged['job_id'])
        self.assert_graph(response, ['decide', 'execute_tool', 'finalize'])
        self.assertEqual(response.presentation['phase'], 'failed')
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertFalse(response.tool_calls[0]['ok'])
        self.assertIsNone(response.presentation['plan'])

    def test_bad_json_disables_model_for_remaining_graph_nodes(self):
        staged = self.service.create('alice', mode='person', stream=photo(),
                                     consent=True, defer=True)
        planner = Mock()
        planner.decide.return_value = 'malformed model output with private-provider-detail'
        self.controller.styling_planner = planner
        response = self.turn('夏季通勤搭配，预算300', attachment_id=staged['job_id'])
        self.assert_graph(response, ['decide', 'execute_tool', 'decide', 'execute_tool', 'finalize'])
        self.assertEqual(planner.decide.call_count, 1)
        self.assertTrue(response.presentation['planner_fallback'])
        self.assertEqual(response.presentation['decision_mode'], 'rule')
        self.assertTrue(response.presentation['plan']['outfits'])
        self.assertNotIn('private-provider-detail', str(response))

    def test_missing_budget_finishes_with_question_and_no_tool(self):
        self.controller.styling_planner = SequencePlanner(
            {'action': 'ask', 'question_fields': ['budget']})
        response = self.turn('帮我通勤搭配')
        self.assert_graph(response, ['decide', 'finalize'])
        self.assertIn('预算', response.text)
        self.assertEqual(response.tool_calls, [])

    def test_decision_limit_stops_after_photo_without_a_second_model_call(self):
        staged = self.service.create('alice', mode='person', stream=photo(),
                                     consent=True, defer=True)
        planner = SequencePlanner({'action': 'inspect_outfit_photo'},
                                  {'action': 'recommend_outfit'})
        self.controller.styling_planner = planner
        with patch.object(StylingWorkflow, 'MAX_DECISIONS', 1):
            response = self.turn('夏季通勤搭配，预算300', attachment_id=staged['job_id'])
        self.assert_graph(response, ['decide', 'execute_tool', 'step_limit', 'finalize'])
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual([call['tool'] for call in response.tool_calls], ['inspect_outfit_photo'])
        self.assertEqual(response.presentation['phase'], 'failed')
        self.assertIsNone(response.presentation['plan'])
        self.assertIn('执行上限', response.text)

    def test_tool_limit_prevents_recommendation_after_successful_photo(self):
        staged = self.service.create('alice', mode='person', stream=photo(),
                                     consent=True, defer=True)
        planner = SequencePlanner({'action': 'inspect_outfit_photo'},
                                  {'action': 'recommend_outfit'})
        self.controller.styling_planner = planner
        with patch.object(StylingWorkflow, 'MAX_TOOLS', 1):
            response = self.turn('夏季通勤搭配，预算300', attachment_id=staged['job_id'])
        self.assert_graph(response, ['decide', 'execute_tool', 'decide', 'step_limit', 'finalize'])
        self.assertEqual([call['tool'] for call in response.tool_calls], ['inspect_outfit_photo'])
        self.assertLessEqual(len(planner.contexts), 2)
        self.assertEqual(response.presentation['phase'], 'failed')
        self.assertIsNone(response.presentation['plan'])
        self.assertIn('执行上限', response.text)

    def test_foreign_photo_cannot_leak_or_poison_owners_next_graph(self):
        staged = self.service.create('alice', mode='person', stream=photo(),
                                     consent=True, defer=True)
        self.controller.styling_planner = SequencePlanner({'action': 'inspect_outfit_photo'})
        foreign = self.turn('夏季通勤搭配，预算300', owner='bob', attachment_id=staged['job_id'])
        self.assert_graph(foreign, ['decide', 'execute_tool', 'finalize'])
        self.assertFalse(foreign.tool_calls[0]['ok'])
        self.assertIsNone(foreign.presentation['plan'])
        self.assertIsNotNone(self.service._jobs[staged['job_id']]['image'])

        self.controller.styling_planner = SequencePlanner(
            {'action': 'inspect_outfit_photo'}, {'action': 'recommend_outfit'})
        own = self.turn('夏季通勤搭配，预算300', attachment_id=staged['job_id'])
        self.assert_graph(own, ['decide', 'execute_tool', 'decide', 'execute_tool', 'finalize'])
        self.assertTrue(own.presentation['plan']['outfits'])
        self.assertFalse(own.presentation['planner_fallback'])
        self.assertIsNone(self.service._jobs[staged['job_id']]['image'])


class ReusedCompiledStylingGraphTests(unittest.TestCase):
    @staticmethod
    def run_workflow(workflow, owner, **changes):
        values = {'text': '夏季通勤搭配，预算300', 'owner': owner,
                  'attachment_id': None, 'options': {'scene': '通勤', 'budget': 300},
                  'observation': None, 'current': None, 'state': {}}
        values.update(changes)
        return workflow.run(**values)

    def test_reused_graph_starts_fresh_after_photo_and_recommendation(self):
        registry = Mock()
        registry.execute.side_effect = [
            {'ok': True, 'data': {'observation': {'mode': 'person', 'garments': [
                {'category': 'tops', 'color': '白色', 'style': '简约', 'fit': '宽松', 'pattern': '纯色'}]}}},
            {'ok': True, 'message': '推荐完成', 'data': {'outfits': []}},
        ]
        workflow = StylingWorkflow(registry)
        graph = workflow.graph
        first = self.run_workflow(workflow, 'alice', attachment_id='alice-photo')
        self.assertEqual(first.workflow_engine, 'langgraph')
        self.assertEqual(len(first.executions), 2)
        self.assertIsNotNone(first.observation)

        second = self.run_workflow(workflow, 'bob', text='帮我搭配', options={})
        self.assertIs(workflow.graph, graph)
        self.assertEqual(second.action, 'ask')
        self.assertEqual(second.waiting, ['scene', 'budget'])
        self.assertEqual(second.options, {})
        self.assertIsNone(second.observation)
        self.assertIsNone(second.result)
        self.assertEqual(second.executions, [])
        self.assertEqual(second.graph_nodes, ['decide', 'finalize'])
        self.assertEqual(registry.execute.call_count, 2)
        self.assertEqual(len(first.executions), 2)
        self.assertEqual(first.graph_nodes, ['decide', 'execute_tool', 'decide', 'execute_tool', 'finalize'])

    def test_concurrent_invocations_of_one_compiled_graph_keep_owner_and_budget(self):
        barrier = threading.Barrier(2)
        registry = Mock()

        def execute(name, **arguments):
            self.assertEqual(name, 'recommend_outfit')
            barrier.wait(timeout=5)
            return {'ok': True, 'message': '推荐完成', 'data': {
                'owner': arguments['demo_session_id'], 'options': copy.deepcopy(arguments['options'])}}

        registry.execute.side_effect = execute
        workflow = StylingWorkflow(registry)
        cases = [('alice', 250), ('bob', 400)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.run_workflow, workflow, owner,
                                       options={'scene': '通勤', 'budget': budget})
                       for owner, budget in cases]
            outcomes = [future.result(timeout=10) for future in futures]
        for (owner, budget), outcome in zip(cases, outcomes):
            self.assertEqual(outcome.result['data']['owner'], owner)
            self.assertEqual(outcome.options['budget'], budget)
            self.assertEqual(outcome.result['data']['options']['budget'], budget)
            self.assertEqual(outcome.graph_nodes, ['decide', 'execute_tool', 'finalize'])
            self.assertEqual(len(outcome.executions), 1)

    def _assert_private_graph_tracing_isolation(self, explicit_tracer=False):
        recorder = RecordingCallback()
        fake_client = Mock(spec=langsmith.Client)
        tracer = LangChainTracer(client=fake_client, project_name='public-parent-test')
        registry = Mock()
        registry.execute.side_effect = [
            {'ok': True, 'data': {'observation': {'mode': 'person', 'garments': [
                {'category': 'tops', 'color': '白色', 'style': '简约', 'fit': '宽松', 'pattern': '纯色'}]}}},
            {'ok': True, 'message': '推荐完成', 'data': {'outfits': []}},
        ]
        workflow = StylingWorkflow(registry)
        markers = ['private-owner-714', 'private-photo-925', 'private-note-836']
        parent_contexts = []

        def public_parent(value):
            before = langsmith.get_tracing_context()
            inherited_tracer = tracing_v2_callback_var.get()
            outcome = self.run_workflow(workflow, markers[0], text=markers[2],
                                        attachment_id=markers[1])
            after = langsmith.get_tracing_context()
            parent_contexts.extend([before, after])
            self.assertIs(tracing_v2_callback_var.get(), inherited_tracer)
            self.assertFalse(outcome.failed)
            self.assertEqual(len(outcome.executions), 2)
            # This benign child runs after the private graph: inherited parent
            # callbacks/tracing must still work here rather than remain disabled.
            return RunnableLambda(lambda payload: payload).invoke({'public_after': True})

        outside_context = langsmith.get_tracing_context()
        outside_tracer = tracing_v2_callback_var.get()
        original_start = LangChainTracer.on_chain_start
        original_end = LangChainTracer.on_chain_end
        with patch.dict(os.environ, {'LANGSMITH_TRACING': 'true', 'LANGCHAIN_TRACING_V2': 'true'}):
            with patch.object(LangChainTracer, 'on_chain_start', autospec=True,
                              side_effect=original_start) as traced_start:
                with patch.object(LangChainTracer, 'on_chain_end', autospec=True,
                                  side_effect=original_end) as traced_end:
                    with langsmith.tracing_context(enabled=True, project_name='public-parent-test',
                                                   metadata={'public_marker': 'parent'}, client=fake_client):
                        trace_scope = (tracing_v2_enabled(project_name='public-parent-test', client=fake_client)
                                       if explicit_tracer else nullcontext(tracer))
                        with trace_scope as active_tracer:
                            result = RunnableLambda(public_parent).invoke(
                                {'public_before': True}, config={'callbacks': [recorder, active_tracer]})
                            if explicit_tracer:
                                self.assertIs(tracing_v2_callback_var.get(), active_tracer)

        self.assertEqual(result, {'public_after': True})
        self.assertEqual(recorder.inputs, [{'public_before': True}, {'public_after': True}])
        self.assertEqual(recorder.outputs, [{'public_after': True}, {'public_after': True}])
        self.assertEqual(traced_start.call_count, 2)
        self.assertEqual(traced_end.call_count, 2)
        self.assertEqual(fake_client.create_run.call_count, 2)
        self.assertEqual(fake_client.update_run.call_count, 2)
        received_payloads = repr((recorder.inputs, recorder.outputs, traced_start.call_args_list,
                                  traced_end.call_args_list, fake_client.mock_calls))
        for marker in markers:
            self.assertNotIn(marker, received_payloads)
        self.assertEqual(parent_contexts[0], parent_contexts[1])
        self.assertTrue(parent_contexts[1]['enabled'])
        self.assertEqual(langsmith.get_tracing_context(), outside_context)
        self.assertIs(tracing_v2_callback_var.get(), outside_tracer)

    def test_parent_tracing_and_callbacks_never_receive_private_graph_state(self):
        self._assert_private_graph_tracing_isolation()

    def test_explicit_parent_langchain_tracer_is_isolated_and_restored(self):
        self._assert_private_graph_tracing_isolation(explicit_tracer=True)


if __name__ == '__main__':
    unittest.main()
