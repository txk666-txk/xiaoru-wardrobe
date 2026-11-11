from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.controller import AgentController
from agent.execution import MiniReActExecutor, ReActAction, ReActDecision
from agent.reflection import ReflectionGuardrail
from agent.router import Router
from agent.tools import ToolRegistry
from agent.tools.common import matches_requested_gender
from agent.providers import DemoCatalogProvider
from agent.store.repository import DemoCommerceRepository
from agent.store.service import CommerceService
from scripts.reset_ecommerce_memory import _validated_user_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = Router()

    def test_routes_product_question(self):
        route = self.router.route("这件西装有什么尺码？")
        self.assertEqual(route.intent, "product_query")
        self.assertEqual(route.strategy, "tool")

    def test_routes_logistics_with_order_id(self):
        route = self.router.route("订单A10001现在到哪里了？")
        self.assertEqual(route.intent, "logistics_query")
        self.assertEqual(route.strategy, "react")
        self.assertEqual(route.entities["order_id"], "A10001")

    def test_routes_customer_facing_fx_order_id(self):
        route = self.router.route("订单 FX-20260929-1A2B3C4D 物流到哪里了？")
        self.assertEqual(route.intent, "logistics_query")
        self.assertEqual(route.entities["order_id"], "FX-20260929-1A2B3C4D")

    def test_routes_complex_after_sales(self):
        route = self.router.route("耳机坏了，而且已经超过7天，还能退款吗？")
        self.assertEqual(route.intent, "after_sales")
        self.assertEqual(route.strategy, "plan_and_solve")

    def test_routes_high_risk_action(self):
        route = self.router.route("帮我退款 A10001")
        self.assertEqual(route.intent, "after_sales_action")

    def test_routes_refund_consultation_as_policy(self):
        route = self.router.route("如何申请退款，需要什么条件？")
        self.assertEqual(route.intent, "policy_query")

    def test_declining_purchase_is_not_routed_to_sales(self):
        self.assertEqual(self.router.route("我暂时不想买了").intent, "general")

    def test_natural_outfit_requests_enter_styling_with_or_without_measurements(self):
        for phrase in ("搭一套", "搭一身", "配一套", "配一身"):
            for prefix in ("", "我身高178体重80kg，"):
                with self.subTest(phrase=phrase, measurements=bool(prefix)):
                    route = self.router.route(prefix + f"帮我{phrase}秋季日常休闲男装，整套预算500元")
                    self.assertEqual(route.intent, "styling")
                    self.assertEqual(route.strategy, "plan_and_solve")
                    if prefix:
                        self.assertEqual(route.entities["height_cm"], "178")
                        self.assertEqual(route.entities["weight_kg"], "80")

    def test_outfit_markers_preserve_order_logistics_and_after_sales_priority(self):
        for phrase in ("搭一套", "搭一身", "配一套", "配一身"):
            for request, expected in (("查订单A10001", "order_query"),
                                      ("订单A10001物流到哪了", "logistics_query"),
                                      ("订单A10001衣服破损", "after_sales"),
                                      ("帮我退款A10001", "after_sales_action")):
                with self.subTest(phrase=phrase, request=request):
                    route = self.router.route(f"我身高178体重80kg，{phrase}之前先{request}")
                    self.assertEqual(route.intent, expected)

    def test_measurement_only_clothing_question_still_requests_size_recommendation(self):
        route = self.router.route("我身高178体重80kg，适合穿什么衣服和裤子")
        self.assertEqual(route.intent, "size_recommendation")


class ToolTests(unittest.TestCase):
    def setUp(self):
        # These scenarios describe the original commerce fixture, independent
        # of the licensed photo catalog shown by a running application.
        original_samples = DemoCatalogProvider(PROJECT_ROOT / "agent" / "data" / "products.json")
        provider_patch = patch("agent.tools.product_tool.create_default_product_provider", return_value=original_samples)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)
        self.registry = ToolRegistry()

    def test_product_query(self):
        # EC 编号保留原有沙盒结构，普通查询使用当前配置目录。
        result = self.registry.execute("query_product", query="EC1003 西装M码有货吗", product_id="EC1003")
        self.assertTrue(result["ok"])
        self.assertIn("商务修身西装外套", result["message"])
        self.assertIn("库存", result["message"])

    def test_generic_recommendation_filters_catalog_by_budget_and_preference(self):
        result = self.registry.execute("query_product", query="推荐秋季宽松外套，预算300元以内")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["items"]), 2)
        for item in result["data"]["items"]:
            self.assertEqual(item["category"], "外套")
            self.assertLessEqual(item["price"], 300)
        self.assertIn("虚构样例", result["message"])
        self.assertNotIn("YZREF-", result["message"])

    def test_explicit_gender_filters_generic_recommendations(self):
        result = self.registry.execute("query_product", query="推荐女装秋季外套")
        self.assertTrue(result["ok"])
        items = result["data"]["items"]
        self.assertTrue(items)
        self.assertTrue(all("女" in item["name"] or "情侣" in item["name"] or "中性" in item["name"] for item in items))

    def test_style_words_do_not_override_opposite_gender_label(self):
        self.assertFalse(matches_requested_gender("男友风女款外套", "男"))
        self.assertFalse(matches_requested_gender("男款秋季外套女", "女"))
        self.assertTrue(matches_requested_gender("情侣男女同款外套", "女"))

    def test_emotional_shopping_request_gets_grounded_recommendations(self):
        result = self.registry.execute("query_product", query="我心情很差，想买件通勤外套，预算300元")
        self.assertTrue(result["ok"])
        self.assertIn("不太顺心", result["message"])
        self.assertTrue(all(item["category"] == "外套" and item["price"] <= 300 for item in result["data"]["items"]))
        self.assertNotIn("一定会开心", result["message"])

    def test_missing_order_id(self):
        result = self.registry.execute("query_order", order_id=None, user_id="User")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_order_id")

    def test_order_query(self):
        result = self.registry.execute("query_order", order_id="A10001", user_id="User")
        self.assertTrue(result["ok"])
        self.assertIn("已发货", result["message"])

    def test_order_privacy_boundary(self):
        result = self.registry.execute("query_order", order_id="A10003", user_id="User")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "order_user_mismatch")

    def test_logistics_query_uses_estimate(self):
        result = self.registry.execute("query_logistics", order_id="A10001", user_id="User")
        self.assertTrue(result["ok"])
        self.assertIn("预计", result["message"])
        self.assertNotIn("一定送达", result["message"])

    def test_logistics_rejects_other_user(self):
        result = self.registry.execute("query_logistics", order_id="A10001", user_id="demo-other-user")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "order_user_mismatch")

    def test_seven_day_policy(self):
        result = self.registry.execute("query_policy", query="七天无理由退货运费谁出")
        self.assertTrue(result["ok"])
        self.assertIn("法定例外", result["message"])
        self.assertIn("运费", result["message"])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = AgentController()

    def test_general_chat_falls_back_to_fay(self):
        response = self.controller.handle("你好", user_id="User")
        self.assertFalse(response.handled)

    def test_size_recommendation_respects_explicit_gender(self):
        with tempfile.TemporaryDirectory() as temp:
            service = CommerceService(
                provider=DemoCatalogProvider(PROJECT_ROOT / "agent" / "data" / "products.json"),
                repository=DemoCommerceRepository(Path(temp) / "size.db"),
            )
            with patch("agent.tools.size_recommendation_tool.get_commerce_service", return_value=service):
                response = self.controller.handle("我是男生，身高178体重80kg，推荐男装衣服和裤子", user_id="User")
        self.assertTrue(response.handled)
        self.assertEqual(response.intent, "size_recommendation")
        self.assertNotIn("请提供商品编号", response.text)
        self.assertIn("EC100", response.text)
        self.assertTrue(all("男" in name or "情侣" in name or "中性" in name for name in (
            line.split("（EC")[0] for line in response.text.splitlines() if line.startswith("- ")
        )))

    def test_logistics_requires_order_id_each_time(self):
        first = self.controller.handle("查询订单 A10001", user_id="User")
        self.assertTrue(first.handled)
        second = self.controller.handle("物流到哪了", user_id="User")
        self.assertTrue(second.handled)
        self.assertIn("请提供订单号", second.text)
        self.assertEqual([call["tool"] for call in second.tool_calls], ["query_order"])
        self.assertTrue(any(item.startswith("Observation:") for item in second.audit))

    def test_logistics_react_observes_order_before_logistics(self):
        response = self.controller.handle("订单 A10001 物流到哪了", user_id="User")
        self.assertTrue(response.handled)
        self.assertEqual(response.strategy, "react")
        self.assertEqual(
            [call["tool"] for call in response.tool_calls],
            ["query_order", "query_logistics"],
        )
        self.assertEqual(sum(item.startswith("Thought:") for item in response.audit), 2)
        self.assertEqual(sum(item.startswith("Action:") for item in response.audit), 2)
        self.assertEqual(sum(item.startswith("Observation:") for item in response.audit), 2)
        self.assertFalse(any("A10001" in item for item in response.audit))

    def test_logistics_react_stops_after_failed_order_verification(self):
        response = self.controller.handle("订单 A10003 物流到哪了", user_id="User")
        self.assertIn("不属于当前顾客", response.text)
        self.assertEqual([call["tool"] for call in response.tool_calls], ["query_order"])
        self.assertTrue(any("执行已停止" in item for item in response.audit))

    def test_refund_requires_confirmation_and_does_not_execute(self):
        first = self.controller.handle("帮我退款 A10001", user_id="User")
        self.assertTrue(first.requires_confirmation)
        self.assertIn("再次明确回复", first.text)

        second = self.controller.handle("确认处理 A10001", user_id="User")
        self.assertFalse(second.requires_confirmation)
        self.assertIn("尚未接入真实", second.text)
        self.assertNotIn("退款已成功", second.text)

    def test_refund_resumes_when_order_is_supplied_later(self):
        self.assertTrue(self.controller.handle("帮我退款").requires_confirmation)
        verified = self.controller.handle("A10001")
        self.assertEqual(verified.intent, "after_sales_action")
        self.assertEqual([call['tool'] for call in verified.tool_calls], ['query_order'])
        self.assertIn("再次明确回复", verified.text)
        self.assertTrue(verified.requires_confirmation)
        confirmed = self.controller.handle("确认处理 A10001")
        self.assertIn("已记录你对订单", confirmed.text)
        self.assertFalse(confirmed.requires_confirmation)

    def test_order_in_first_confirmation_still_requires_verified_second_confirmation(self):
        self.controller.handle("帮我退款")
        first = self.controller.handle("确认处理 A10001")
        self.assertTrue(first.requires_confirmation)
        self.assertNotIn("已记录你对订单", first.text)
        self.assertIn("再次明确回复", first.text)

    def test_pending_refund_rejects_other_owner_and_can_be_cancelled(self):
        self.controller.handle("帮我退款")
        denied = self.controller.handle("订单号：A10003")
        self.assertIn("不属于当前顾客", denied.text)
        self.assertEqual(self.controller.memory.get_pending_action("User")['order_id'], '')
        self.assertIn("已取消", self.controller.handle("取消本次操作").text)
        self.assertIsNone(self.controller.memory.get_pending_action("User"))

    def test_new_order_requires_verification_and_unrelated_query_does_not_resume(self):
        self.controller.handle("帮我退款 A10001")
        self.assertEqual(self.controller.handle("A10001 物流到哪了").intent, 'logistics_query')
        self.assertIn("不一致", self.controller.handle("确认处理 A10003").text)
        self.assertIn("不属于当前顾客", self.controller.handle("A10003").text)
        self.assertEqual(self.controller.memory.get_pending_action("User")['order_id'], 'A10001')

    def test_pending_action_does_not_cross_browser_commerce_sessions(self):
        self.controller.handle("帮我退款", demo_session_id='browser-a')
        answer = self.controller.handle("A10001", demo_session_id='browser-b')
        self.assertFalse(answer.requires_confirmation)
        self.assertNotEqual(answer.intent, 'after_sales_action')

    def test_ambiguous_confirmation_does_not_confirm_refund(self):
        first = self.controller.handle("帮我退款 A10001", user_id="User")
        self.assertTrue(first.requires_confirmation)

        for message in ("我不确定", "不要确认", "我确定喜欢黑色"):
            response = self.controller.handle(message, user_id="User")
            self.assertTrue(response.requires_confirmation)
            self.assertNotIn("已记录你对订单", response.text)

    def test_failed_order_does_not_authorize_logistics(self):
        first = self.controller.handle("查询订单 A10003", user_id="User")
        self.assertIn("不属于当前顾客", first.text)
        second = self.controller.handle("物流到哪了", user_id="User")
        self.assertIn("请提供订单号", second.text)

    def test_complex_after_sales_returns_plan_and_policy(self):
        response = self.controller.handle("订单 A10002 的耳机坏了，超过7天还能退款吗", user_id="User")
        self.assertTrue(response.handled)
        self.assertEqual(response.strategy, "plan_and_solve")
        self.assertIn("已核对订单", response.text)
        self.assertIn("质量问题", response.text)
        self.assertLessEqual(len(response.text.splitlines()), 4)
        self.assertTrue(any("固定规则规划（非 LLM 自动规划）" in item for item in response.audit))
        self.assertEqual(
            [call["tool"] for call in response.tool_calls],
            ["query_order", "query_policy"],
        )


class MiniReActExecutorTests(unittest.TestCase):
    class Registry:
        def __init__(self):
            self.calls = []

        def execute(self, tool, **arguments):
            self.calls.append((tool, arguments))
            return {"ok": True, "message": "ok", "data": {}}

    def test_hard_step_limit_prevents_unbounded_actions(self):
        registry = self.Registry()
        executor = MiniReActExecutor(registry, max_steps=1)
        action = ReActAction("query_order", {"order_id": "A10001"}, "核验订单")
        outcome = executor.run(
            action,
            decide_next=lambda _action, _result: ReActDecision(next_action=action),
            summarize_observation=lambda _action, _result: "订单核验成功",
        )
        self.assertEqual(len(registry.calls), 1)
        self.assertTrue(outcome.reached_step_limit)
        self.assertIn("安全执行上限", outcome.final_message)

    def test_tool_exception_is_redacted_and_stops_execution(self):
        class FailingRegistry:
            def execute(self, _tool, **_arguments):
                raise RuntimeError("provider secret should not be public")

        executor = MiniReActExecutor(FailingRegistry(), max_steps=2)
        outcome = executor.run(
            ReActAction("query_order", {}, "核验订单"),
            decide_next=lambda _action, _result: ReActDecision(),
            summarize_observation=lambda _action, result: (
                f"工具失败（{result.get('error')}），执行已停止"
            ),
        )
        self.assertTrue(outcome.stopped_on_failure)
        self.assertEqual(len(outcome.executions), 1)
        self.assertNotIn("provider secret", repr(outcome.audit))
        self.assertNotIn("provider secret", outcome.final_message)


class ReflectionTests(unittest.TestCase):
    def test_rewrites_delivery_guarantee(self):
        guard = ReflectionGuardrail()
        reviewed = guard.review("您的包裹保证明天送达。", intent="logistics_query")
        self.assertIn("预计明天送达", reviewed)
        self.assertIn("以物流后续更新为准", reviewed)
        self.assertNotIn("保证明天", reviewed)

    def test_blocks_unverified_write_success(self):
        guard = ReflectionGuardrail()
        reviewed = guard.review("退款已成功。", intent="after_sales_action", evidence={})
        self.assertIn("没有执行真实交易操作", reviewed)


class KnowledgeBaseTests(unittest.TestCase):
    def test_knowledge_files_and_official_sources_exist(self):
        knowledge_dir = PROJECT_ROOT / "agent" / "data" / "knowledge"
        files = sorted(knowledge_dir.glob("*.md"))
        self.assertGreaterEqual(len(files), 9)

        with (PROJECT_ROOT / "agent" / "data" / "policies.json").open("r", encoding="utf-8") as file:
            policies = json.load(file)
        self.assertGreaterEqual(len(policies["sources"]), 8)
        self.assertTrue(all(source["url"].startswith("https://") for source in policies["sources"]))
        self.assertTrue(all(source["accessed_at"] == "2026-09-27" for source in policies["sources"]))

        # Check the distributable defaults, not ignored, user-specific MCP state.
        mcp_data = PROJECT_ROOT / "faymcp" / "data"
        with (mcp_data / "mcp_servers.example.json").open("r", encoding="utf-8") as file:
            servers = json.load(file)
        self.assertTrue(servers)
        self.assertTrue(all(server["autostart"] is False for server in servers))
        self.assertTrue(all(not server["key"] for server in servers))

        with (mcp_data / "mcp_prestart_tools.example.json").open("r", encoding="utf-8") as file:
            prestart_tools = json.load(file)
        self.assertTrue(set(prestart_tools).issubset({str(server["id"]) for server in servers}))
        for tools in prestart_tools.values():
            self.assertTrue(all(tool["allow_function_call"] is False for tool in tools.values()))

    def test_reset_script_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            _validated_user_dir("../User")


if __name__ == "__main__":
    unittest.main()
