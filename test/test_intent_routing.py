"""Primary model routing must preserve business facts, conversation context and permission checks."""

import json
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from agent.controller import AgentController, AgentResponse
from agent.intent_router import (
    ConfiguredIntentClassifier,
    SemanticIntentRouter,
    configured_intent_router,
    parse_intent_decision,
)
from agent.router import RouteResult, Router


def decision(intent="general", confidence=0.95, operation=None, **fields):
    return {"intent": intent, "confidence": confidence, "operation": operation, **fields}


class IntentDecisionTests(unittest.TestCase):
    def test_accepts_json_and_fenced_json(self):
        expected = decision("styling")
        for raw in (expected, json.dumps(expected), "```json\n" + json.dumps(expected) + "\n```"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_intent_decision(raw), expected)

    def test_rejects_unsupported_actions_entities_and_invalid_confidence(self):
        invalid = ["not json", "[]", "x" * 10001, decision("delete_file"),
                   {**decision(), "owner": "another-user"},
                   {**decision(), "entities": {"order_id": "A10001"}},
                   {**decision(), "tool": "refund_order"},
                   {"intent": "general", "confidence": 0.9},
                   decision(intent=[]), decision(confidence=True),
                   decision(confidence=float("nan")), decision(confidence=float("inf")),
                   decision(confidence=-0.1), decision(confidence=1.1),
                   decision(confidence="0.95"), decision(operation="cancel"),
                   decision("after_sales_action"), decision("after_sales_action", operation="pay")]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises((ValueError, TypeError)):
                parse_intent_decision(raw)

    def test_reply_and_parameter_shape_are_checked(self):
        invalid = [decision("general", reply=[]), decision("general", reply="长" * 1201),
                   decision("styling", reply="我已经直接替你买了"),
                   decision("styling", updates=[], evidence={})]
        for output in invalid:
            with self.subTest(output=output), self.assertRaises((ValueError, TypeError)):
                parse_intent_decision(output)


class SemanticRouterTests(unittest.TestCase):
    def setUp(self):
        self.classifier = Mock()
        self.classifier.decide.return_value = decision("order_query")
        self.router = SemanticIntentRouter(self.classifier)
        self.original = RouteResult("general", "fallback", 0.35, {"order_id": "A10001"})

    def test_preserves_only_server_extracted_entities(self):
        result = self.router.route("A10001 这笔现在怎样了", self.original)
        self.assertEqual(result.intent, "order_query")
        self.assertEqual(result.strategy, "tool")
        self.assertEqual(result.entities, {"order_id": "A10001"})
        self.assertIsNot(result.entities, self.original.entities)

    def test_product_category_alias_is_a_grounded_structured_filter(self):
        self.classifier.decide.return_value = decision("product_query", updates={"category": "外套", "budget": 200},
                                                      evidence={"category": "外搭", "budget": "200"})
        result = self.router.route("有没有轻薄些的外搭，预算200", self.original)
        self.assertEqual(result.entities["product_filters"], {"category": "外套", "budget": 200})

    def test_product_category_requires_evidence_from_current_message(self):
        self.classifier.decide.return_value = decision("product_query", updates={"category": "外套"},
                                                      evidence={"category": "外搭"})
        self.assertIsNone(self.router.route("你好", self.original))

    def test_uncertain_general_can_ask_for_clarification_directly(self):
        self.classifier.decide.return_value = decision("general", confidence=0.5, reply="你是想查商品还是调整搭配？")
        result = self.router.route("刚才那个呢", self.original)
        self.assertEqual(result.entities["reply"], "你是想查商品还是调整搭配？")

    def test_model_cannot_turn_a_request_for_cheaper_items_into_unlimited_budget(self):
        self.classifier.decide.return_value = decision("styling", updates={"budget_unlimited": True},
                                                      evidence={"budget_unlimited": "便宜一点"})
        self.assertIsNone(self.router.route("便宜一点", self.original))

    def test_maps_semantic_operations_without_mutating_original_entities(self):
        for operation, expected in (("cancel", "取消"), ("refund", "退款"),
                                    ("return", "退货"), ("change_address", "地址")):
            with self.subTest(operation=operation):
                self.classifier.decide.return_value = decision("after_sales_action", operation=operation)
                result = self.router.route("这笔请照我的要求处理", self.original)
                self.assertEqual(result.entities["operation"], expected)
        self.assertEqual(self.original.entities, {"order_id": "A10001"})

    def test_ambiguous_or_invalid_model_output_returns_no_route(self):
        for output in (decision("styling", 0.74), {**decision(), "product_id": "EC1003"}, "bad json",
                       decision("styling", updates={"owner_id": "other"}, evidence={"owner_id": "other"}),
                       decision("styling", updates={"gender": "invalid"}, evidence={"gender": "invalid"})):
            with self.subTest(output=output):
                self.classifier.decide.return_value = output
                self.assertIsNone(self.router.route("一句含糊的话", self.original))

    def test_styling_updates_require_current_message_evidence(self):
        text = "我是男的，请重新选"
        self.classifier.decide.return_value = decision("styling", updates={"gender": "男款"},
                                                      evidence={"gender": "我是男的"})
        result = self.router.route(text, self.original)
        self.assertEqual(result.entities["styling_updates"], {"gender": "男款"})
        self.classifier.decide.return_value = decision("styling", updates={"gender": "男款"},
                                                      evidence={"gender": "编出来的依据"})
        self.assertIsNone(self.router.route(text, self.original))

    def test_provider_errors_do_not_log_private_details(self):
        self.classifier.decide.side_effect = TimeoutError("private-provider-token")
        with self.assertLogs("agent.intent_router", level="WARNING") as captured:
            self.assertIsNone(self.router.route("私人请求", self.original))
        self.assertNotIn("private-provider-token", " ".join(captured.output))
        self.assertNotIn("私人请求", " ".join(captured.output))

    def test_empty_input_skips_model(self):
        self.assertIsNone(self.router.route("  ", self.original))
        self.classifier.decide.assert_not_called()

    def test_only_bounded_redacted_text_is_sent_to_classifier(self):
        self.router.route("身高178cm，体重65kg，邮件 alice@example.test，手机号13812345678，"
                          "订单A10001，sk-private-token，" + "长" * 1300, self.original)
        context = self.classifier.decide.call_args.args[0]
        self.assertIn("text", context)
        self.assertLessEqual(len(context["text"]), 1200)
        for private in ("178", "65kg", "alice@example.test", "13812345678", "A10001", "sk-private-token"):
            self.assertNotIn(private, context["text"])

    def test_configuration_enables_model_by_default_and_allows_rule_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(configured_intent_router(), SemanticIntentRouter)
        for mode in ("off", "rule", "0"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"FAY_INTENT_ROUTER": mode}):
                self.assertIsNone(configured_intent_router())

    def test_configured_classifier_reuses_existing_provider_with_short_timeout(self):
        from utils import config_util as cfg
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(decision())}}]}
        with patch.multiple(cfg, system_config=object(), gpt_base_url="http://127.0.0.1:9999/v1",
                            gpt_model_engine="test-model", key_gpt_api_key="test-secret"):
            with patch("requests.post", return_value=response) as post:
                output = ConfiguredIntentClassifier().decide({"text": "你好"})
        self.assertEqual(parse_intent_decision(output)["intent"], "general")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:9999/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 15))
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 500)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "test-model")
        self.assertNotIn("test-secret", json.dumps(post.call_args.kwargs["json"]))


class ControllerSemanticRoutingTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.conversation.return_value = None
        self.service.latest.return_value = None
        context = patch("agent.styling.service.get_styling_service", return_value=self.service)
        context.start()
        self.addCleanup(context.stop)
        self.classifier = Mock()
        self.classifier.decide.return_value = decision()
        self.registry = Mock()
        self.registry.execute.return_value = {"ok": False, "message": "未找到记录", "data": {}}
        self.controller = AgentController(registry=self.registry, policy_answerer=Mock(),
                                          intent_router=SemanticIntentRouter(self.classifier))

    def test_unmatched_business_semantics_dispatch_to_existing_handlers(self):
        text = "下星期去公司报到，整身花销别过300"
        self.assertEqual(Router().route(text).intent, "general")
        for intent, handler in (("styling", "_handle_styling"), ("product_query", "_handle_product"),
                                ("size_recommendation", "_handle_size_recommendation"),
                                ("order_query", "_handle_order"), ("logistics_query", "_handle_logistics"),
                                ("policy_query", "_handle_policy"), ("after_sales", "_handle_after_sales")):
            with self.subTest(intent=intent):
                self.classifier.decide.return_value = decision(intent)
                expected = AgentResponse(handled=True, intent=intent)
                with patch.object(self.controller, handler, return_value=expected) as dispatched:
                    result = self.controller.handle(text, demo_session_id="alice")
                self.assertIs(result, expected)
                dispatched.assert_called_once()

    def test_general_classification_leaves_reply_to_fay_without_policy_lookup(self):
        result = self.controller.handle("客服今天心情怎么样？")
        self.assertFalse(result.handled)
        self.assertEqual(result.intent, "general")
        self.registry.execute.assert_not_called()
        self.assertIn("语义路由", result.audit[0])

    def test_general_reply_is_returned_without_another_tool_or_reply_model(self):
        self.classifier.decide.return_value = decision("general", reply="刚才你在找通勤装，我们也可以先聊聊。")
        result = self.controller.handle("先歇会儿", demo_session_id="alice")
        self.assertTrue(result.handled)
        self.assertEqual(result.intent, "general")
        self.assertEqual(result.text, "刚才你在找通勤装，我们也可以先聊聊。")
        self.registry.execute.assert_not_called()
        self.classifier.decide.assert_called_once()

    def test_direct_reply_cannot_claim_an_unexecuted_transaction(self):
        self.classifier.decide.return_value = decision("general", reply="订单已取消")
        result = self.controller.handle("处理好了吗", demo_session_id="alice")
        self.assertTrue(result.handled)
        self.assertIn("没有执行真实交易操作", result.text)
        self.registry.execute.assert_not_called()

    def test_recent_exchange_is_provided_to_next_message_and_is_owner_scoped(self):
        self.classifier.decide.return_value = decision("general", reply="我们刚才在聊通勤。")
        self.controller.handle("聊聊通勤吧", demo_session_id="alice")
        self.classifier.decide.return_value = decision("general", reply="你想继续聊通勤。")
        self.controller.handle("接着说", demo_session_id="alice")
        context = self.classifier.decide.call_args.args[0]
        history = context["history"]
        self.assertTrue(any(message["role"] == "user" and "聊聊通勤" in message["content"]
                            for message in history))
        self.assertTrue(any(message["role"] == "assistant" and "通勤" in message["content"]
                            for message in history))
        self.controller.handle("接着说", demo_session_id="bob")
        other_context = self.classifier.decide.call_args.args[0]
        self.assertFalse(other_context["history"])
        self.assertNotIn("alice", json.dumps(other_context))

    def test_dialogue_history_is_bounded_and_redacted(self):
        self.classifier.decide.return_value = decision("general", reply="继续聊聊。")
        for index in range(7):
            self.controller.handle(f"第{index}轮，手机号13812345678，邮箱alice@example.test，" + "长" * 400,
                                   demo_session_id="alice")
        context = self.classifier.decide.call_args.args[0]
        self.assertLessEqual(len(context["history"]), 8)
        for item in context["history"]:
            self.assertLessEqual(len(item["content"]), 350)
            self.assertNotIn("13812345678", item["content"])
            self.assertNotIn("alice@example.test", item["content"])

    def test_model_failure_preserves_existing_general_and_local_policy_fallback(self):
        self.classifier.decide.side_effect = TimeoutError("private-token")
        result = self.controller.handle("给我讲个笑话吧")
        self.assertFalse(result.handled)
        self.assertNotIn("private-token", str(result))
        self.registry.execute.assert_not_called()
        self.controller.handle("多少天之内能寄回去？")
        self.registry.execute.assert_called_once_with("query_policy", query="多少天之内能寄回去？", concise=True)

    def test_keyword_requests_also_use_primary_model_routing(self):
        for text, intent, handler in (("帮我通勤搭配，预算300", "styling", "_handle_styling"),
                                      ("查询订单 A10001", "order_query", "_handle_order"),
                                      ("这件西装多少钱", "product_query", "_handle_product"),
                                      ("帮我退款 A10001", "after_sales_action", "_handle_high_risk_action")):
            with self.subTest(text=text):
                operation = "refund" if intent == "after_sales_action" else None
                self.classifier.decide.return_value = decision(intent, operation=operation)
                result = AgentResponse(handled=True, text="已有业务处理结果", intent=intent)
                with patch.object(self.controller, handler, return_value=result) as dispatched:
                    self.assertIs(self.controller.handle(text), result)
                dispatched.assert_called_once()
        self.assertEqual(self.classifier.decide.call_count, 4)

    def test_model_can_override_a_misleading_keyword(self):
        text = "我是男的，给女朋友买外套"
        self.classifier.decide.return_value = decision("product_query")
        expected = AgentResponse(handled=True, text="女款外套候选", intent="product_query")
        with patch.object(self.controller, "_handle_product", return_value=expected) as product:
            result = self.controller.handle(text, demo_session_id="alice")
        self.assertIs(result, expected)
        product.assert_called_once()
        self.classifier.decide.assert_called_once()

    def test_model_failure_keeps_keyword_business_route(self):
        self.classifier.decide.side_effect = TimeoutError("private-token")
        expected = AgentResponse(handled=True, text="已核验订单", intent="order_query")
        with patch.object(self.controller, "_handle_order", return_value=expected) as order:
            result = self.controller.handle("查询订单 A10001", demo_session_id="alice")
        self.assertIs(result, expected)
        order.assert_called_once()
        self.classifier.decide.assert_called_once()

    def test_complete_outfit_survives_router_timeout_and_product_only_misclassification(self):
        text = "帮我推荐一套衣服，上衣要棕色的外套，内搭要白色，裤子颜色随意"
        self.assertEqual(Router().route(text).intent, "styling")
        expected = AgentResponse(handled=True, text="整套搭配结果", intent="styling")
        with patch.object(self.controller, "_handle_styling", return_value=expected) as styling:
            self.classifier.decide.side_effect = TimeoutError()
            self.assertIs(self.controller.handle(text), expected)
            self.classifier.decide.side_effect = None
            self.classifier.decide.return_value = decision("product_query")
            self.assertIs(self.controller.handle(text), expected)
        self.assertEqual(styling.call_count, 2)
        self.registry.execute.assert_not_called()

    def test_known_styling_followup_is_classified_with_its_task_state(self):
        self.service.conversation.return_value = {"active": True, "waiting": ["budget"],
                                                   "options": {"scene": "通勤"}}
        self.classifier.decide.return_value = decision("styling")
        expected = AgentResponse(handled=True, text="已按250元筛选", intent="styling")
        with patch.object(self.controller, "_handle_styling", return_value=expected):
            result = self.controller.handle("250", demo_session_id="alice")
        self.assertIs(result, expected)
        context = self.classifier.decide.call_args.args[0]
        self.assertEqual(context["styling"]["options"]["scene"], "通勤")
        self.assertEqual(context["styling"]["waiting"], ["budget"])

    def test_attachment_keeps_its_verified_styling_path(self):
        expected = AgentResponse(handled=True, text="已看图", intent="styling")
        with patch.object(self.controller, "_handle_styling", return_value=expected) as styling:
            result = self.controller.handle("看看这张图", demo_session_id="alice", attachment_id="photo-1")
        self.assertIs(result, expected)
        styling.assert_called_once()

    def test_opaque_followup_can_switch_from_styling_to_another_business(self):
        self.service.conversation.return_value = {"active": True, "waiting": ["budget"]}
        self.controller.styling_planner = Mock()
        self.classifier.decide.return_value = decision("order_query")
        with patch.object(self.controller, "_handle_order") as order, patch.object(self.controller, "_handle_styling") as styling:
            self.controller.handle("之前那笔还在处理吗", demo_session_id="alice")
        order.assert_called_once()
        styling.assert_not_called()
        self.classifier.decide.assert_called_once()

    def test_model_recipient_gender_reaches_product_handler(self):
        text = "我是男的，给女朋友买外套"
        self.classifier.decide.return_value = decision("product_query", updates={"gender": "女款"},
                                                      evidence={"gender": "给女朋友买外套"})
        expected = AgentResponse(handled=True, text="女款外套候选", intent="product_query")
        with patch.object(self.controller, "_handle_product", return_value=expected) as product:
            self.controller.handle(text, demo_session_id="alice")
        self.assertEqual(product.call_args.args[2].entities["gender"], "女款")

    def test_semantic_product_request_still_verifies_product_card(self):
        self.classifier.decide.return_value = decision("product_query")
        for verified in (None, "EC1003"):
            with self.subTest(verified=verified):
                with patch.object(self.controller, "verified_product_context_id", return_value=verified) as check:
                    with patch.object(self.controller, "_handle_product") as product:
                        result = self.controller.handle("这件能机洗吗", product_id="EC1003")
                check.assert_called_once_with("EC1003")
                if verified is None:
                    product.assert_not_called()
                    self.assertTrue(result.handled)
                    self.assertIn("本地商品目录", result.text)
                else:
                    self.assertEqual(product.call_args.args[2].entities["product_id"], "EC1003")

    def test_semantic_product_request_can_reuse_verified_recent_product(self):
        self.classifier.decide.return_value = decision("product_query")
        owner = self.controller._memory_user_id("User", "alice")
        self.controller.memory.remember(owner, "product_query", product_id="EC1003")
        with patch.object(self.controller, "_handle_product") as product:
            self.controller.handle("这件能机洗吗", demo_session_id="alice")
        self.assertEqual(product.call_args.args[2].entities["product_id"], "EC1003")

    def test_semantic_cancellation_requires_confirmation_and_preserves_operation(self):
        self.classifier.decide.return_value = decision("after_sales_action", operation="cancel")
        result = self.controller.handle("这笔不要了", demo_session_id="alice")
        self.assertTrue(result.requires_confirmation)
        self.assertEqual(result.intent, "after_sales_action")
        owner = self.controller._memory_user_id("User", "alice")
        self.assertEqual(self.controller.memory.get_pending_action(owner)["operation"], "取消")
        self.registry.execute.assert_not_called()

    def test_semantic_order_lookup_uses_original_id_and_current_owner(self):
        self.classifier.decide.return_value = decision("order_query")
        text = "A10001 之前这笔还在处理吗"
        self.assertEqual(Router().route(text).intent, "general")
        result = self.controller.handle(text, user_id="customer", demo_session_id="alice")
        self.registry.execute.assert_called_once_with("query_order", order_id="A10001",
                                                       user_id="customer", demo_session_id="alice")
        self.assertEqual(result.intent, "order_query")
        self.assertEqual(result.text, "未找到记录")

    def test_pending_confirmation_and_cancel_cannot_be_overridden_by_model(self):
        owner = self.controller._memory_user_id("User", "alice")
        for text in ("确认处理 A10001", "算了", "我确认一下"):
            with self.subTest(text=text):
                self.controller.memory.set_pending_action(owner, {"order_id": "A10001", "operation": "取消"})
                with patch.object(self.controller, "_handle_high_risk_action") as action:
                    result = self.controller.handle(text, demo_session_id="alice")
                if text.startswith("确认处理"):
                    action.assert_called_once()
                elif text == "算了":
                    self.assertIsNone(self.controller.memory.get_pending_action(owner))
                else:
                    self.assertTrue(result.requires_confirmation)
        self.classifier.decide.assert_not_called()

    def test_normal_fay_entry_constructs_the_configured_semantic_router(self):
        from agent import fay_adapter
        with patch.object(fay_adapter, "_controller", None):
            with patch("agent.styling.planner.configured_planner", return_value=None):
                with patch("agent.intent_router.configured_intent_router", return_value=self.controller.intent_router):
                    with patch.object(fay_adapter, "AgentController", return_value=self.controller) as factory:
                        with patch.object(self.controller, "handle", return_value=AgentResponse(handled=False)):
                            fay_adapter.handle_text("你好")
        self.assertIs(factory.call_args.kwargs["intent_router"], self.controller.intent_router)


class FirstTurnStylingIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test.test_styling_conversation import DialogueTests
        DialogueTests.setUp(self)
        classifier = Mock()
        classifier.decide.return_value = decision("styling")
        self.classifier = classifier
        self.controller.intent_router = SemanticIntentRouter(classifier)
        self.controller.styling_planner = Mock()
        self.controller.styling_planner.decide.return_value = {
            "action": "recommend_outfit", "updates": {"scene": "通勤", "budget": 300},
            "evidence": {"scene": "去公司报到", "budget": "300"}}

    def tearDown(self):
        from test.test_styling_conversation import DialogueTests
        DialogueTests.tearDown(self)

    def test_first_colloquial_request_reaches_graph_and_returns_real_catalog_cards(self):
        text = "下星期去公司报到，整身花销别过300"
        self.assertEqual(Router().route(text).intent, "general")
        response = self.controller.handle(text, demo_session_id="alice")
        self.classifier.decide.assert_called_once()
        self.assertTrue(response.handled)
        self.assertEqual(response.intent, "styling")
        self.assertEqual(response.presentation["workflow_engine"], "langgraph")
        self.assertEqual([call["tool"] for call in response.tool_calls], ["recommend_outfit"])
        plan = response.presentation["plan"]
        self.assertEqual(plan["options"]["scene"], "通勤")
        self.assertEqual(plan["options"]["budget"], 300)
        self.assertTrue(plan["outfits"])
        self.assertTrue(all(outfit["total"] <= 300 for outfit in plan["outfits"]))


class ContextualCorrectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        from test.test_styling_conversation import DialogueTests
        from test.test_styling import Catalog, product
        from agent.styling.engine import StylingEngine
        DialogueTests.setUp(self)
        products = [product("M_TOP", "T恤/上衣", 80, "棕色男款通勤上衣"),
                    product("F_TOP", "T恤/上衣", 70, "棕色女款通勤上衣"),
                    product("M_BOTTOM", "裤装", 100, "黑色男款直筒裤"),
                    product("F_BOTTOM", "裤装", 90, "黑色女款直筒裤")]
        self.service.engine = StylingEngine(Catalog(products))
        self.classifier = Mock()
        self.controller.intent_router = SemanticIntentRouter(self.classifier)
        self.controller.styling_planner = Mock()
        self.controller.styling_planner.decide.return_value = {"action": "recommend_outfit"}
        options = {"scene": "通勤", "budget": 300, "style": "简约",
                   "required_colors_by_group": {"tops": ["棕"]}}
        plan = self.service.engine.plan(options)
        self.service.remember_plan("alice", plan)
        self.service.update_conversation("alice", options=plan["options"], active=True,
                                         waiting=[], personal_asked=True)

    def tearDown(self):
        from test.test_styling_conversation import DialogueTests
        DialogueTests.tearDown(self)

    def test_gender_correction_recommends_again_and_preserves_original_constraints(self):
        text = "我是男的，你这搭配怎么又有男装又有女装"
        self.classifier.decide.return_value = decision("styling", updates={"gender": "男款"},
                                                      evidence={"gender": "我是男的"})
        response = self.controller.handle(text, demo_session_id="alice")
        self.assertTrue(response.handled)
        self.assertEqual(response.intent, "styling")
        self.assertIn("recommend_outfit", [call["tool"] for call in response.tool_calls])
        plan = response.presentation["plan"]
        self.assertEqual(plan["options"]["gender"], "男款")
        self.assertEqual(plan["options"]["scene"], "通勤")
        self.assertEqual(plan["options"]["budget"], 300)
        self.assertEqual(plan["options"]["required_colors_by_group"], {"tops": ["棕"]})
        self.assertTrue(plan["outfits"])
        for outfit in plan["outfits"]:
            self.assertLessEqual(outfit["total"], 300)
            self.assertTrue(all(item["product_id"] in {"M_TOP", "M_BOTTOM"} for item in outfit["items"]))
        context = self.classifier.decide.call_args.args[0]
        self.assertEqual(context["styling"]["options"]["budget"], 300)
        self.assertTrue(context["styling"]["current_outfit"])

    def test_recipient_request_switches_to_product_tool_instead_of_revising_own_outfit(self):
        text = "我是男的，给女朋友买外套"
        self.classifier.decide.return_value = decision("product_query")
        expected = AgentResponse(handled=True, text="女款外套查询结果", intent="product_query")
        with patch.object(self.controller, "_handle_product", return_value=expected) as product:
            with patch.object(self.controller, "_handle_styling") as styling:
                result = self.controller.handle(text, demo_session_id="alice")
        self.assertIs(result, expected)
        product.assert_called_once()
        styling.assert_not_called()


class ProductFilterIntegrationTests(unittest.TestCase):
    def test_semantic_alias_uses_structured_category_gender_and_budget(self):
        from agent.providers.demo_catalog import DemoCatalogProvider
        from agent.tools.product_tool import ProductTool
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(json.dumps({"products": [
                {"product_id": "M1", "name": "基础轻薄外套", "category": "外套", "price": 80, "gender": "male"},
                {"product_id": "F1", "name": "基础轻薄外套", "category": "外套", "price": 70, "gender": "female"},
                {"product_id": "F2", "name": "基础轻薄外套", "category": "外套", "price": 150, "gender": "female"},
            ]}), encoding="utf-8")
            tool = ProductTool()
            tool.public_provider = DemoCatalogProvider(path)
            result = tool.execute(query="有没有轻薄些的外搭", filters={"category": "外套", "gender": "女款", "budget": 100})
        self.assertTrue(result["ok"])
        self.assertEqual([item["product_id"] for item in result["data"]["items"]], ["F1"])

    def test_invalid_filters_cannot_execute_an_unbounded_catalog_query(self):
        from agent.tools.product_tool import ProductTool
        tool = ProductTool()
        for filters in ({"category": "../secret"}, {"budget": True}, {"owner": "someone"}):
            with self.subTest(filters=filters):
                self.assertFalse(tool.execute(query="外搭", filters=filters)["ok"])


if __name__ == "__main__":
    unittest.main()
