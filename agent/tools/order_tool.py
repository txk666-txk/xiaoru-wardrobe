from __future__ import annotations

from agent.store.repository import CommerceRepositoryError
from agent.store.service import get_commerce_service

from .common import load_json, tool_result


class OrderTool:
    name = "query_order"
    description = "按订单号查询沙盒订单状态、商品、金额和配送信息"

    def __init__(self) -> None:
        payload = load_json("orders.json")
        self.dataset_label = payload.get("dataset_label", "演示订单数据")
        self.orders = {item["order_id"].upper(): item for item in payload.get("orders", [])}

    @staticmethod
    def _storage_order_id(order_id: str) -> str:
        normalized = order_id.upper()
        return f"DEMO-{normalized[3:]}" if normalized.startswith("FX-") else normalized

    @staticmethod
    def _display_order_id(order_id: str) -> str:
        normalized = order_id.upper()
        return f"FX-{normalized[5:]}" if normalized.startswith("DEMO-") else normalized

    def execute(
        self,
        *,
        order_id: str | None = None,
        user_id: str = "User",
        demo_session_id: str | None = None,
        **_: object,
    ) -> dict:
        if not order_id:
            return tool_result(
                ok=False,
                error="missing_order_id",
                message="请提供订单号后我再帮你查询，例如 FX-20260929-1A2B3C4D。为保护隐私，请不要发送支付密码或完整银行卡信息。",
            )

        normalized_order_id = self._storage_order_id(order_id)
        if normalized_order_id.startswith("DEMO-"):
            if not demo_session_id:
                return tool_result(
                    ok=False,
                    error="missing_demo_session",
                    message="这是沙盒订单，请在创建该订单的同一浏览器会话中查询。",
                )
            try:
                order = get_commerce_service().get_order(demo_session_id, normalized_order_id)
            except CommerceRepositoryError as exc:
                return tool_result(ok=False, error=exc.code, message=str(exc))
            item_text = "、".join(
                f"{item['name']}×{item['quantity']}（"
                + (
                    " / ".join(f"{key}{value}" for key, value in item.get("attributes", {}).items())
                    if item.get("attributes")
                    else "规格待确认" if item.get("specification_pending") else "公开快照商品"
                )
                + "）"
                for item in order.get("items", [])
            )
            message = (
                f"订单 {self._display_order_id(order['order_id'])} 状态为“{order['status_text']}”，"
                f"创建时间 {order['created_at']}，商品为 {item_text}，沙盒钱包支付 ￥{order['total']:.2f}。"
                f"配送路线为{order['shipping']['from']}至{order['shipping']['to']}。"
                "该订单属于当前业务沙盒，不会同步到外部店铺。"
            )
            return tool_result(ok=True, message=message, data=order)

        order = self.orders.get(normalized_order_id)
        if not order:
            return tool_result(
                ok=False,
                error="order_not_found",
                message=f"当前{self.dataset_label}中没有找到订单 {order_id.upper()}，请核对订单号。",
            )
        if order.get("user_id") != user_id:
            return tool_result(
                ok=False,
                error="order_user_mismatch",
                message="该订单不属于当前顾客，无法展示订单详情。请核对登录账号或转接人工客服。",
            )

        item_text = "、".join(
            f"{item['product_name']}×{item['quantity']}（{item.get('spec', '默认规格')}）"
            for item in order.get("items", [])
        )
        message = (
            f"当前{self.dataset_label}中，订单 {order['order_id']} 状态为“{order['status_text']}”，"
            f"下单时间 {order['created_at']}，商品为 {item_text}，实付 ￥{order['paid_amount']:.2f}。"
        )
        return tool_result(ok=True, message=message, data=order)
