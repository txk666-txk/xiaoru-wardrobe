from __future__ import annotations

from agent.store.repository import CommerceRepositoryError
from agent.store.service import get_commerce_service

from .common import load_json, tool_result


class LogisticsTool:
    name = "query_logistics"
    description = "按订单号查询沙盒物流轨迹、配送路线和预计送达信息"

    def __init__(self) -> None:
        payload = load_json("logistics.json")
        self.dataset_label = payload.get("dataset_label", "演示物流数据")
        self.records = {item["order_id"].upper(): item for item in payload.get("logistics", [])}
        order_payload = load_json("orders.json")
        self.orders = {item["order_id"].upper(): item for item in order_payload.get("orders", [])}

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
                message="请提供订单号后我再帮你查询物流，例如 FX-20260929-1A2B3C4D。",
            )
        normalized_order_id = self._storage_order_id(order_id)
        if normalized_order_id.startswith("DEMO-"):
            if not demo_session_id:
                return tool_result(
                    ok=False,
                    error="missing_demo_session",
                    message="这是沙盒订单，请在创建该订单的同一浏览器会话中查询物流。",
                )
            try:
                data = get_commerce_service().get_logistics(demo_session_id, normalized_order_id)
            except CommerceRepositoryError as exc:
                return tool_result(ok=False, error=exc.code, message=str(exc))
            events = data.get("events") or []
            latest = events[0] if events else {}
            latest_description = str(latest.get("description", "")).rstrip("。！？.!? ")
            latest_text = (
                f"最新进展：{latest.get('event_time', '')} {latest_description}。"
                if latest
                else "当前还没有新的物流事件。"
            )
            message = (
                f"订单 {self._display_order_id(normalized_order_id)} 的状态为“{data['status_text']}”。"
                f"路线为{data['route']['from']}至{data['route']['to']}。"
                f"{latest_text}当前为业务沙盒配送流程，时间仅供体验参考。"
            )
            return tool_result(ok=True, message=message, data=data)

        order = self.orders.get(normalized_order_id)
        if not order:
            return tool_result(
                ok=False,
                error="order_not_found",
                message=f"当前演示订单数据中没有找到订单 {normalized_order_id}，请核对订单号。",
            )
        if order.get("user_id") != user_id:
            return tool_result(
                ok=False,
                error="order_user_mismatch",
                message="该订单不属于当前顾客，无法展示物流轨迹。请核对登录账号或转接人工客服。",
            )

        record = self.records.get(normalized_order_id)
        if not record:
            return tool_result(
                ok=False,
                error="logistics_not_found",
                message=f"当前{self.dataset_label}中没有找到订单 {order_id.upper()} 的物流记录。",
            )

        latest = record.get("events", [{}])[0]
        estimate = record.get("estimated_delivery")
        estimate_text = f"，预计 {estimate} 送达" if estimate else ""
        message = (
            f"当前{self.dataset_label}显示，订单 {record['order_id']} 的包裹状态为“{record['status_text']}”。"
            f"最新进展：{latest.get('time', '')} {latest.get('description', '暂无更新')}"
            f"{estimate_text}。预计时间可能受天气、交通和网点处理影响，请以物流后续更新为准。"
        )
        return tool_result(ok=True, message=message, data=record)
