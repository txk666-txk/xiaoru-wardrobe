from __future__ import annotations


ORDER_STATUS_LABELS = {
    "demo_created": "待发货",
    "awaiting_demo_fulfillment": "待发货",
    "shipped": "已发货",
    "in_transit": "运输中",
    "delivered": "已签收",
    "closed": "已完成",
    "cancelled": "已取消",
}

ORDER_TRANSITIONS = {
    "demo_created": {"awaiting_demo_fulfillment", "cancelled"},
    "awaiting_demo_fulfillment": {"shipped", "cancelled"},
    "shipped": {"in_transit"},
    "in_transit": {"delivered"},
    "delivered": {"closed"},
    "closed": set(),
    "cancelled": set(),
}

RETURN_STATUS_LABELS = {
    "submitted": "售后申请已提交",
    "reviewing": "售后审核中",
    "approved": "退货申请已通过",
    "rejected": "退货申请未通过",
    "return_in_transit": "退货运输中",
    "received": "退货已收货",
    "demo_refund_completed": "退款已退回沙盒钱包",
}

RETURN_TRANSITIONS = {
    "submitted": {"reviewing"},
    "reviewing": {"approved", "rejected"},
    "approved": {"return_in_transit"},
    "rejected": set(),
    "return_in_transit": {"received"},
    "received": {"demo_refund_completed"},
    "demo_refund_completed": set(),
}


class InvalidStateTransition(ValueError):
    pass


def validate_order_transition(current: str, target: str) -> None:
    if current not in ORDER_TRANSITIONS:
        raise InvalidStateTransition(f"未知订单状态：{current}")
    if target not in ORDER_TRANSITIONS[current]:
        raise InvalidStateTransition(f"订单不能从 {current} 变更为 {target}")


def validate_return_transition(current: str, target: str) -> None:
    if current not in RETURN_TRANSITIONS:
        raise InvalidStateTransition(f"未知售后状态：{current}")
    if target not in RETURN_TRANSITIONS[current]:
        raise InvalidStateTransition(f"售后不能从 {current} 变更为 {target}")
