"""演示店铺的售后分流规则；不代替消费者法定权利判断。"""

from __future__ import annotations

from collections.abc import Mapping


LARGE_ORDER_ITEM_COUNT = 10
LARGE_ORDER_AMOUNT_CNY = 5000


def needs_large_order_review(order: Mapping) -> bool:
    """Use verified order totals, never a customer's unverified description."""
    count = order.get("item_count")
    if count is None:
        count = sum(int(item.get("quantity") or 0) for item in order.get("items") or [])
    amount = order.get("total")
    if amount is None:
        amount = order.get("paid_amount")
    return int(count or 0) >= LARGE_ORDER_ITEM_COUNT or float(amount or 0) >= LARGE_ORDER_AMOUNT_CNY
