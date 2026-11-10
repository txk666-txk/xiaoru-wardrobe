from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from agent.providers import ProductProvider, create_default_product_provider
from agent.service_rules import needs_large_order_review

from .repository import DemoCommerceRepository


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime" / "ecommerce_demo.db"
RETURN_REASON_CODES = {
    "no_longer_needed",
    "size_not_suitable",
    "quality_issue",
    "damaged",
    "wrong_item",
    "missing_parts",
    "other",
}


class CommerceServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CommerceService:
    def __init__(
        self,
        *,
        provider: ProductProvider | None = None,
        repository: DemoCommerceRepository | None = None,
    ) -> None:
        self.provider = provider or create_default_product_provider()
        db_path = Path(os.environ.get("FAY_COMMERCE_DB_PATH") or DEFAULT_DB_PATH)
        self.repository = repository or DemoCommerceRepository(db_path)

    def storefront(self, *, session_id: str | None = None) -> dict[str, Any]:
        profile = self.provider.get_store_profile()
        catalog = self.provider.search_products(page=1, page_size=50)
        products = catalog["items"]
        orders = self.repository.list_orders(session_id) if session_id else []
        account = self.repository.get_account(session_id) if session_id else None
        logistics = [
            {
                "order_id": order["order_id"],
                "status": order["status"],
                "status_text": order["status_text"],
                "carrier": "沙盒物流",
                "tracking_no_masked": "SANDBOX-ONLY",
                "estimated_delivery": None,
                "route": order.get("shipping"),
                "events": [
                    {"time": event["event_time"], "description": event["description"]}
                    for event in order.get("logistics", [])
                ],
            }
            for order in orders
        ]
        source_url = profile.get("source_url")
        return {
            "store": {
                "name": profile.get("store_name") or "小如的衣橱体验店",
                "subtitle": profile.get("subtitle") or "授权商品目录 · 沙盒交易流程",
                "dataset_label": profile.get("dataset_label") or "本地商品目录",
                "reference_url": source_url,
                "snapshot_at": profile.get("snapshot_at"),
                "ownership_verified": False,
                "notice": profile.get("transaction_notice") or "付款、物流与退款仅在沙盒钱包中模拟。",
            },
            "products": [self._compat_product(product) for product in products],
            "orders": [self._compat_order(order) for order in orders],
            "logistics": logistics,
            "account": account,
            "sources": [
                {
                    "title": profile.get("store_name") or "本地商品目录",
                    "publisher": profile.get("store_name") or "本地商品目录",
                    "url": source_url,
                    "accessed_at": profile.get("snapshot_at"),
                    "notice": profile.get("dataset_label") or "商品目录由运营者自行配置。",
                }
            ],
        }

    @staticmethod
    def _compat_product(product: dict[str, Any]) -> dict[str, Any]:
        highlights = []
        if product.get("selling_point"):
            highlights.append(str(product["selling_point"]))
        highlights.extend(str(item) for item in product.get("service_labels_public") or [])
        if not highlights:
            highlights = list(product.get("highlights") or ["本地演示商品；不代表真实在售商品"])
        skus = []
        for variant in product.get("variants") or []:
            skus.append(
                {
                    "sku_id": variant.get("sku_id"),
                    "attributes": variant.get("attributes") or {},
                    "stock": variant.get("stock_public"),
                    "price": variant.get("price"),
                    "disabled": variant.get("disabled_public", False),
                }
            )
        return {
            "product_id": product["product_id"],
            "name": product["name"],
            "aliases": [product.get("source_product_alias")],
            "category": product.get("category") or "其他",
            "price": product.get("price"),
            "price_max": product.get("price_max"),
            "currency": product.get("currency") or "CNY",
            "highlights": highlights,
            "image": product.get("image_url"),
            "image_url": product.get("image_url"),
            "image_attribution": product.get("image_attribution"),
            "source_url": product.get("source_url"),
            "snapshot_at": product.get("snapshot_at"),
            "stock_total_public": product.get("stock_total_public"),
            "skus": skus,
            "demo_only": True,
            "tryon_supported": bool(product.get("tryon_supported")),
        }

    @staticmethod
    def _compat_order(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": order["order_id"],
            "user_id": "demo-session",
            "status": order["status"],
            "status_text": order["status_text"],
            "created_at": order["created_at"],
            "paid_amount": order["total"],
            "payment_status": order.get("payment_status"),
            "refunded_amount": order.get("refunded_amount", 0),
            "wallet_type": "sandbox",
            "shipping": order.get("shipping"),
            "logistics_no_masked": "SANDBOX-ONLY",
            "items": [
                {
                    "product_id": item["product_id"],
                    "product_name": item["name"],
                    "quantity": item["quantity"],
                    "spec": (
                        " / ".join(f"{key}:{value}" for key, value in item.get("attributes", {}).items())
                        if item.get("attributes")
                        else "规格待确认" if item.get("specification_pending") else "目录商品"
                    ),
                    "sku_id": item.get("sku_id"),
                    "specification_pending": item.get("specification_pending", False),
                    "order_item_id": item["order_item_id"],
                }
                for item in order.get("items", [])
            ],
            "demo_only": True,
        }

    def list_products(self, **kwargs: Any) -> dict[str, Any]:
        return self.provider.search_products(**kwargs)

    def get_product(self, product_id: str) -> dict[str, Any]:
        product = self.provider.get_product(product_id)
        if not product:
            raise CommerceServiceError("product_not_found", "未找到该目录商品")
        return product

    def get_cart(self, session_id: str) -> dict[str, Any]:
        return self.repository.get_cart(session_id)

    def get_account(self, session_id: str) -> dict[str, Any]:
        return self.repository.get_account(session_id)

    def set_cart_item(
        self,
        session_id: str,
        product_id: str,
        quantity: int,
        sku_id: str | None = None,
    ) -> dict[str, Any]:
        product = self.get_product(product_id)
        quantity = int(quantity)
        selected_variant = None
        normalized_sku = str(sku_id or "").strip().upper()
        if normalized_sku:
            selected_variant = next(
                (
                    variant
                    for variant in product.get("variants") or []
                    if str(variant.get("sku_id") or "").upper() == normalized_sku
                ),
                None,
            )
            if selected_variant is None:
                if quantity == 0:
                    selected_variant = {"sku_id": normalized_sku, "attributes": {}}
                else:
                    raise CommerceServiceError("sku_not_found", "该目录商品没有此规格")
            if quantity > 0 and selected_variant.get("disabled_public"):
                raise CommerceServiceError("sku_unavailable", "该规格当前不可选")
            stock = selected_variant.get("stock_public")
            if quantity > 0 and stock is not None and quantity > int(stock):
                raise CommerceServiceError("insufficient_public_stock", "数量超过目录中该规格的库存")
        elif quantity > 0 and not product.get("variants"):
            stock = product.get("stock_total_public")
            if stock is not None and quantity > int(stock):
                raise CommerceServiceError("insufficient_public_stock", "数量超过目录中的商品总库存")
        return self.repository.set_cart_item(
            session_id,
            product,
            quantity,
            selected_variant=selected_variant,
        )

    def preview_order(self, session_id: str) -> dict[str, Any]:
        return self.repository.create_order_preview(session_id)

    def create_order(
        self,
        session_id: str,
        confirmation_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.repository.create_order(session_id, confirmation_token, idempotency_key)

    def list_orders(self, session_id: str) -> list[dict[str, Any]]:
        return self.repository.list_orders(session_id)

    def get_order(self, session_id: str, order_id: str) -> dict[str, Any]:
        return self.repository.get_order(session_id, order_id)

    def get_logistics(self, session_id: str, order_id: str) -> dict[str, Any]:
        return self.repository.get_logistics(session_id, order_id)

    def advance_order(self, session_id: str, order_id: str) -> dict[str, Any]:
        """按固定 DEMO 状态机前进一步，不接受调用方指定目标状态。"""
        order = self.repository.get_order(session_id, order_id)
        next_status = {
            "demo_created": "awaiting_demo_fulfillment",
            "awaiting_demo_fulfillment": "shipped",
            "shipped": "in_transit",
            "in_transit": "delivered",
            "delivered": "closed",
        }.get(order["status"])
        if not next_status:
            raise CommerceServiceError("order_cannot_advance", "当前订单已是终态，不能继续推进")
        return self.repository.transition_order(session_id, order_id, next_status)

    def cancel_order(self, session_id: str, order_id: str) -> dict[str, Any]:
        return self.repository.cancel_order(session_id, order_id)

    def preview_return(
        self,
        session_id: str,
        *,
        order_id: str,
        order_item_id: int,
        quantity: int,
        reason_code: str,
    ) -> dict[str, Any]:
        if reason_code not in RETURN_REASON_CODES:
            raise CommerceServiceError("invalid_reason", "请选择有效的演示退货原因")
        preview = self.repository.create_return_preview(
            session_id,
            order_id,
            int(order_item_id),
            int(quantity),
            reason_code,
        )
        preview["manual_review_required"] = needs_large_order_review(self.get_order(session_id, order_id))
        if preview["manual_review_required"]:
            preview["confirmation_text"] = "确认提交沙盒大单退货申请，后续须由人工复核（不会影响真实店铺订单）"
        return preview

    def _with_review_notice(self, session_id: str, request: dict[str, Any]) -> dict[str, Any]:
        result = dict(request)
        result["manual_review_required"] = needs_large_order_review(self.get_order(session_id, result["order_id"]))
        if result["manual_review_required"] and result["status"] == "submitted":
            result["status_text"] = "沙盒大单申请待人工复核（未接入真实人工渠道）"
        return result

    def create_return(
        self,
        session_id: str,
        confirmation_token: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request = self.repository.create_return_request(
            session_id,
            confirmation_token,
            idempotency_key,
        )
        return self._with_review_notice(session_id, request)

    def list_returns(self, session_id: str) -> list[dict[str, Any]]:
        return [self._with_review_notice(session_id, item) for item in self.repository.list_return_requests(session_id)]

    def get_return(self, session_id: str, return_id: str) -> dict[str, Any]:
        return self._with_review_notice(session_id, self.repository.get_return_request(session_id, return_id))

    def advance_return(self, session_id: str, return_id: str) -> dict[str, Any]:
        """按固定沙盒售后状态机推进；退款完成时仓储层原子返还余额。"""
        request = self.repository.get_return_request(session_id, return_id)
        next_status = {
            "submitted": "reviewing",
            "reviewing": "approved",
            "approved": "return_in_transit",
            "return_in_transit": "received",
            "received": "demo_refund_completed",
        }.get(request["status"])
        if not next_status:
            raise CommerceServiceError("return_cannot_advance", "当前售后已是终态，不能继续推进")
        return self._with_review_notice(
            session_id,
            self.repository.transition_return(session_id, return_id, next_status),
        )

    @staticmethod
    def _number(value: object, name: str, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise CommerceServiceError("invalid_measurement", f"{name}需要填写数字") from exc
        if number < minimum or number > maximum:
            raise CommerceServiceError(
                "invalid_measurement",
                f"{name}超出可处理范围（{minimum:g}–{maximum:g}）",
            )
        return number

    @staticmethod
    def _verified_size_rows(product: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        chart = product.get("size_chart")
        if isinstance(chart, dict) and chart.get("verified") is True:
            rows = chart.get("weight_ranges") or chart.get("entries") or []
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)], chart
        ranges = product.get("weight_ranges")
        if product.get("size_chart_verified") is True and isinstance(ranges, list):
            return [row for row in ranges if isinstance(row, dict)], {
                "verified": True,
                "source": product.get("size_chart_source"),
            }
        return [], {}

    def recommend_size(
        self,
        *,
        product_id: str,
        height_cm: object,
        weight_kg: object,
        chest_cm: object | None = None,
        shoulder_cm: object | None = None,
        waist_cm: object | None = None,
        hip_cm: object | None = None,
        fit_preference: str | None = None,
    ) -> dict[str, Any]:
        """纯计算，不写数据库、日志、SessionMemory 或长期记忆。"""
        product = self.get_product(product_id)
        rows, chart = self._verified_size_rows(product)
        if not rows:
            raise CommerceServiceError(
                "size_chart_unavailable",
                "该商品当前没有已核验的专属尺码表，不能仅凭身高体重给出具体尺码。",
            )
        measurements: dict[str, float] = {
            "height_cm": self._number(height_cm, "身高", 120, 220),
            "weight_kg": self._number(weight_kg, "体重", 30, 200),
        }
        optional_specs = (
            ("chest_cm", chest_cm, "胸围", 50, 180),
            ("shoulder_cm", shoulder_cm, "肩宽", 25, 80),
            ("waist_cm", waist_cm, "腰围", 45, 180),
            ("hip_cm", hip_cm, "臀围", 55, 200),
        )
        for key, value, label, minimum, maximum in optional_specs:
            if value not in (None, ""):
                measurements[key] = self._number(value, label, minimum, maximum)

        def in_range(row: dict[str, Any], key: str, value: float) -> bool:
            minimum = row.get(f"min_{key}")
            maximum = row.get(f"max_{key}")
            if minimum is None and maximum is None:
                return True
            if minimum is not None and value < float(minimum):
                return False
            if maximum is not None and value > float(maximum):
                return False
            return True

        demo_chart = chart.get("verification_scope") == "fictional_demo_size_chart"
        matches = [
            row
            for row in rows
            if int(row.get("stock_public", 1)) > 0
            and all(in_range(row, key, value) for key, value in measurements.items())
        ]
        if not matches:
            return {
                "product_id": product["product_id"],
                "product_name": product["name"],
                "recommendations": [],
                "confidence": "low",
                "message": ("沙盒演示尺码范围未覆盖这些数据，不适用于真实购买。" if demo_chart
                            else "已核验尺码表中没有同时覆盖这些数据的尺码，建议人工核对围度或试穿。"),
                "fit_preference": fit_preference,
                "source": chart.get("source"),
                "basis": chart.get("verification_scope") or "verified_size_chart",
                "chart_notice": chart.get("notice"),
                "privacy_notice": "身体参数仅用于本次计算，未写入数据库或长期记忆。",
            }
        sizes = [str(row.get("size") or row.get("label") or "") for row in matches]
        sizes = [size for size in sizes if size]
        confidence = "medium" if len(sizes) == 1 else "low"
        used_optional_measurement = any(
            key in measurements
            and (matches[0].get(f"min_{key}") is not None or matches[0].get(f"max_{key}") is not None)
            for key in ("chest_cm", "shoulder_cm", "waist_cm", "hip_cm")
        ) if len(matches) == 1 else False
        if len(sizes) == 1 and used_optional_measurement:
            confidence = "high"
        return {
            "product_id": product["product_id"],
            "product_name": product["name"],
            "recommendations": sizes,
            "confidence": confidence,
            "message": (
                f"按{'沙盒演示尺码范围' if demo_chart else '该商品已核验尺码表'}，候选尺码为：{'、'.join(sizes)}。"
                "线上建议仅供参考，实际穿着还会受体型和版型偏好影响。"
            ),
            "fit_preference": fit_preference,
            "source": chart.get("source"),
            "basis": chart.get("verification_scope") or "verified_size_chart",
            "chart_notice": chart.get("notice"),
            "privacy_notice": "身体参数仅用于本次计算，未写入数据库或长期记忆。",
        }


_service: CommerceService | None = None
_service_lock = threading.Lock()


def get_commerce_service() -> CommerceService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CommerceService()
    return _service


def set_commerce_service_for_tests(service: CommerceService | None) -> None:
    global _service
    with _service_lock:
        _service = service
