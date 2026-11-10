from __future__ import annotations

import unittest

from agent.controller import AgentController
from agent.router import Router
from agent.service_rules import needs_large_order_review
from agent.store.service import set_commerce_service_for_tests
from test.test_commerce_backend import CommerceFixture


class LargeOrderReviewTests(unittest.TestCase):
    def test_thresholds_use_verified_order_count_or_amount(self):
        self.assertFalse(needs_large_order_review({"item_count": 9, "total": 4999.99}))
        self.assertTrue(needs_large_order_review({"item_count": 10, "total": 100}))
        self.assertTrue(needs_large_order_review({"items": [{"quantity": 1}], "paid_amount": 5000}))

    def test_bulk_return_policy_question_does_not_route_to_order_lookup(self):
        self.assertEqual(Router().route("大型订单退货需要转人工吗").intent, "policy_query")
        answer = AgentController().handle("大型订单退货需要转人工吗")
        self.assertIn("满10件", answer.text)
        self.assertIn("人工复核", answer.text)
        self.assertLessEqual(len(answer.text.splitlines()), 3)

    def test_large_demo_return_remains_pending_human_review(self):
        fixture = CommerceFixture()
        try:
            session = "large-return-session"
            fixture.repository.set_wallet_balance_for_tests(session, 500_000)
            fixture.service.set_cart_item(session, "YZREF-ABC123", 10)
            preview = fixture.service.preview_order(session)
            order = fixture.service.create_order(session, preview["confirmation_token"], "large-order")
            for status in ("shipped", "in_transit", "delivered"):
                order = fixture.repository.transition_order(session, order["order_id"], status)
            return_preview = fixture.service.preview_return(
                session,
                order_id=order["order_id"],
                order_item_id=order["items"][0]["order_item_id"],
                quantity=1,
                reason_code="size_not_suitable",
            )
            self.assertTrue(return_preview["manual_review_required"])
            request = fixture.service.create_return(session, return_preview["confirmation_token"], "large-return")
            self.assertTrue(request["manual_review_required"])
            self.assertEqual(request["status"], "submitted")
            self.assertIn("人工复核", request["status_text"])
            self.assertTrue(fixture.service.list_returns(session)[0]["manual_review_required"])
            set_commerce_service_for_tests(fixture.service)
            controller = AgentController()
            consult = controller.handle(
                f"订单 {order['order_id']} 我要退货",
                user_id="User",
                demo_session_id=session,
            )
            self.assertEqual(consult.intent, "after_sales")
            self.assertIn("人工复核", consult.text)
            action = controller.handle(
                f"帮我退货 {order['order_id']}",
                user_id="User",
                demo_session_id=session,
            )
            self.assertFalse(action.requires_confirmation)
            self.assertIn("人工复核", action.text)
            self.assertIn("没有执行退款", action.text)
            controller.handle("帮我退货", demo_session_id=session)
            continued = controller.handle(order['order_id'], demo_session_id=session)
            self.assertIn("人工复核", continued.text)
            self.assertFalse(continued.requires_confirmation)
        finally:
            set_commerce_service_for_tests(None)
            fixture.close()


if __name__ == "__main__":
    unittest.main()
