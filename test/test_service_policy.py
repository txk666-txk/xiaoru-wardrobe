from __future__ import annotations

import unittest

from agent.tools.policy_tool import PolicyTool


class ServicePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tool = PolicyTool()

    def test_large_order_return_escalates_without_denial(self):
        result = self.tool.execute(query="大型订单退货需要转人工吗")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["policies"][0]["policy_id"], "large_order_return_review")
        message = result["message"]
        self.assertIn("10件", message)
        self.assertIn("5000元", message)
        self.assertIn("转人工", message)
        self.assertIn("不自动拒绝", message)
        self.assertIn("不延后、取消或缩减", message)
        self.assertIn("演示店铺政策", message)

    def test_service_conduct_is_available_for_emotional_and_sales_requests(self):
        for query in ("如何给顾客提供情绪价值", "怎么宣传商品并推荐商品"):
            with self.subTest(query=query):
                result = self.tool.execute(query=query)
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"]["policies"][0]["policy_id"], "service_conduct")
                self.assertIn("维护店铺正当利益", result["message"])
                self.assertIn("不虚构", result["message"])

    def test_seven_day_return_and_quality_remain_distinct(self):
        no_reason = self.tool.execute(policy_id="seven_day_no_reason")
        self.assertTrue(no_reason["ok"])
        self.assertEqual(no_reason["data"]["policies"][0]["scope"], "legal_baseline")
        self.assertIn("法定例外", no_reason["message"])
        self.assertIn("合理拆封", no_reason["message"])
        self.assertIn("消费者承担", no_reason["message"])

        quality = self.tool.execute(policy_id="quality_after_sales")
        self.assertTrue(quality["ok"])
        self.assertIn("运输等必要费用", quality["message"])
        self.assertIn("不能仅因超过七日", quality["message"])


if __name__ == "__main__":
    unittest.main()
