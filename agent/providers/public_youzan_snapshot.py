"""本地有赞公开页面快照 Provider。

本模块从不联网。快照由显式运行的只读同步脚本生成，不能用于查询真实订单、
执行支付、退款或修改店铺数据。
"""

from __future__ import annotations

import copy
import json
import math
import re
import threading
from urllib.parse import urlparse
from collections import Counter
from pathlib import Path
from typing import Any

from agent.catalog_images import has_catalog_image
from .base import ProductProvider


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _local_product_id(alias: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(alias or "")).upper()
    return f"YZREF-{normalized}" if normalized else ""


def _local_sku_id(source_sku_id: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "", str(source_sku_id or "")).upper()
    return f"YZSKU-{normalized}" if normalized else ""


_WEIGHT_RANGE_RE = re.compile(
    r"^(?P<size>[A-Za-z0-9+]+)\s*(?:(?P<lower>\d+(?:\.\d+)?)\s*[-~至]\s*(?P<upper>\d+(?:\.\d+)?)\s*斤|(?P<under>\d+(?:\.\d+)?)\s*斤以下|(?P<over>\d+(?:\.\d+)?)\s*斤以上)$",
    re.IGNORECASE,
)


def _public_weight_ranges(variants: list[dict[str, Any]], source_url: str | None) -> list[dict[str, Any]]:
    """从公开 SKU 的明确尺码文字提取体重范围，不推测未展示的数据。"""
    by_range: dict[tuple[str, float | None, float | None], dict[str, Any]] = {}
    for variant in variants:
        attributes = variant.get("attributes") or {}
        raw_size = next(
            (str(value).strip() for key, value in attributes.items() if "尺码" in str(key) or "大小" in str(key)),
            "",
        )
        match = _WEIGHT_RANGE_RE.fullmatch(raw_size)
        if not match:
            continue
        lower = match.group("lower") or match.group("over")
        upper = match.group("upper") or match.group("under")
        minimum = float(lower) / 2 if lower is not None else None
        maximum = float(upper) / 2 if upper is not None else None
        key = (match.group("size").upper(), minimum, maximum)
        record = by_range.setdefault(
            key,
            {
                "size": key[0],
                "min_weight_kg": minimum,
                "max_weight_kg": maximum,
                "public_label": raw_size,
                "stock_public": 0,
                "source": source_url,
            },
        )
        stock = variant.get("stock_public")
        if stock is not None and not variant.get("disabled_public"):
            record["stock_public"] += max(int(stock), 0)
    return [
        {key: value for key, value in record.items() if value is not None}
        for record in by_range.values()
    ]


class PublicYouzanSnapshotProvider(ProductProvider):
    """读取 `products_youzan_public.json` 并输出稳定的标准化结构。"""

    def __init__(
        self,
        *,
        products_path: Path | str | None = None,
        profile_path: Path | str | None = None,
    ) -> None:
        self.products_path = Path(products_path or DATA_DIR / "products_youzan_public.json")
        self.profile_path = Path(profile_path or DATA_DIR / "store_profile.json")
        self._lock = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._profile: dict[str, Any] = {}
        self._products: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._refresh_if_needed(force=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"商品快照不存在：{path}")
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict):
            raise ValueError(f"商品快照格式无效：{path}")
        return payload

    def _current_signature(self) -> tuple[int, int]:
        return (
            self.products_path.stat().st_mtime_ns if self.products_path.exists() else -1,
            self.profile_path.stat().st_mtime_ns if self.profile_path.exists() else -1,
        )

    @staticmethod
    def _normalize_variant(variant: dict[str, Any]) -> dict[str, Any]:
        source_sku_id = variant.get("source_sku_id") or variant.get("sku_id")
        price = variant.get("price")
        normalized: dict[str, Any] = {
            "sku_id": _local_sku_id(source_sku_id),
            "source_sku_id": str(source_sku_id) if source_sku_id is not None else None,
            "attributes": dict(variant.get("attributes") or {}),
            "price": float(price) if price is not None else None,
            "currency": variant.get("currency") or "CNY",
            "stock_public": variant.get("stock_public"),
            "disabled_public": bool(variant.get("disabled_public", False)),
        }
        return {key: value for key, value in normalized.items() if value is not None and value != ""}

    @classmethod
    def _normalize_product(
        cls,
        product: dict[str, Any],
        dataset: dict[str, Any],
    ) -> dict[str, Any]:
        alias = str(
            product.get("source_product_alias")
            or product.get("external_alias")
            or product.get("alias")
            or ""
        )
        product_id = product.get("product_id") or _local_product_id(alias)
        if not product_id:
            raise ValueError("公开商品缺少可用的 alias/product_id")

        price = product.get("price")
        variants = [
            cls._normalize_variant(item)
            for item in (product.get("variants") or product.get("skus") or [])
            if isinstance(item, dict)
        ]
        source_url = product.get("source_url") or product.get("public_url")
        image_url = str(product.get("image_url") or "").strip()
        image_location = urlparse(image_url)
        has_photo = bool(
            image_location.scheme == "https" and image_location.hostname
            and not image_location.path.lower().endswith(".svg")
        ) or has_catalog_image(image_url)
        public_weight_ranges = _public_weight_ranges(variants, source_url)
        normalized: dict[str, Any] = {
            "product_id": str(product_id).upper(),
            "source_product_alias": alias,
            "source_product_id": product.get("source_product_id"),
            "name": str(product.get("name") or "未命名商品"),
            "subtitle": product.get("subtitle"),
            "selling_point": product.get("selling_point"),
            "category": product.get("category") or "其他",
            "category_source": product.get("category_source"),
            "price": float(price) if price is not None else None,
            "price_max": float(product["price_max"]) if product.get("price_max") is not None else None,
            "currency": product.get("currency") or "CNY",
            "image_url": product.get("image_url"),
            "image_attribution": copy.deepcopy(product.get("image_attribution") or {}),
            "tryon_supported": has_photo and product.get("tryon_supported") is not False,
            "tryon_image_url": product.get("tryon_image_url"),
            "tryon_category": product.get("tryon_category"),
            "tryon_garment_photo_type": product.get("tryon_garment_photo_type") or "model",
            "color_label": product.get("color_label"),
            "aliases": list(product.get("aliases") or []),
            "picture_urls": list(product.get("picture_urls") or []),
            "public_url": product.get("public_url") or product.get("source_url"),
            "source_url": source_url,
            "source_type": product.get("source_type") or dataset.get("source_type"),
            "snapshot_at": product.get("snapshot_at") or dataset.get("snapshot_at"),
            "ownership_verified": bool(product.get("ownership_verified", False)),
            "detail_enriched": bool(product.get("detail_enriched", False)),
            "stock_total_public": product.get("stock_total_public"),
            "stock_source": product.get("stock_source"),
            "sold_count_public": product.get("sold_count_public"),
            "specification_groups": list(product.get("specification_groups") or []),
            "variants": variants,
            "category_properties_public": product.get("category_properties_public"),
            "service_labels_public": list(product.get("service_labels_public") or []),
            "support_unconditional_return_public": product.get("support_unconditional_return_public"),
            "support_quick_refund_public": product.get("support_quick_refund_public"),
            "support_barter_public": product.get("support_barter_public"),
            "data_notice": product.get("data_notice") or "本地公开页面快照，非商家 API 实时数据；图片与资料须由配置者确认使用权限",
        }
        explicit_chart = product.get("size_chart")
        if isinstance(explicit_chart, dict) and explicit_chart.get("verified") is True:
            normalized["size_chart"] = copy.deepcopy(explicit_chart)
            normalized["size_chart_verified"] = True
            normalized["size_chart_source"] = explicit_chart.get("source")
            explicit_ranges = explicit_chart.get("weight_ranges") or explicit_chart.get("entries")
            if isinstance(explicit_ranges, list):
                normalized["weight_ranges"] = copy.deepcopy(explicit_ranges)
        elif public_weight_ranges:
            normalized["weight_ranges"] = public_weight_ranges
            normalized["size_chart_verified"] = True
            normalized["size_chart_source"] = source_url
            normalized["size_chart"] = {
                "verified": True,
                "verification_scope": "public_embedded_sku_weight_label_only",
                "source": source_url,
                "weight_ranges": public_weight_ranges,
                "notice": "仅依据公开 SKU 中明确展示的体重范围，不包含身高、围度或版型保证。",
            }
        return {key: value for key, value in normalized.items() if value is not None}

    def _refresh_if_needed(self, *, force: bool = False) -> None:
        with self._lock:
            signature = self._current_signature()
            if not force and signature == self._signature:
                return
            dataset = self._read_json(self.products_path)
            profile = self._read_json(self.profile_path)
            raw_products = dataset.get("products")
            if not isinstance(raw_products, list):
                raise ValueError("商品快照缺少 products 数组")
            products = [
                self._normalize_product(item, dataset)
                for item in raw_products
                if isinstance(item, dict)
            ]
            by_id = {item["product_id"]: item for item in products}
            if len(by_id) != len(products):
                raise ValueError("商品快照包含重复的本地 product_id")
            self._profile = dict(profile)
            self._profile.setdefault("dataset_label", dataset.get("dataset_label"))
            self._profile.setdefault("snapshot_at", dataset.get("snapshot_at"))
            self._profile.setdefault("ownership_verified", False)
            self._profile.setdefault(
                "transaction_notice",
                "当前仅使用本地商品目录和沙盒交易，不会在真实交易平台生成订单。",
            )
            self._products = products
            self._by_id = by_id
            self._signature = signature

    def get_store_profile(self) -> dict[str, Any]:
        self._refresh_if_needed()
        with self._lock:
            profile = copy.deepcopy(self._profile)
            profile["catalog_items_available"] = len(self._products)
            return profile

    def list_categories(self) -> list[dict[str, Any]]:
        self._refresh_if_needed()
        with self._lock:
            counts = Counter(item.get("category") or "其他" for item in self._products)
        return [
            {"name": category, "count": count}
            for category, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

    @staticmethod
    def _matches_query(product: dict[str, Any], query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            str(value or "")
            for value in (
                product.get("product_id"),
                product.get("name"),
                product.get("subtitle"),
                product.get("selling_point"),
                product.get("category"),
            )
        ).lower()
        tokens = [token for token in re.split(r"\s+", query.lower().strip()) if token]
        return all(token in haystack for token in tokens)

    def search_products(
        self,
        *,
        query: str = "",
        category: str | None = None,
        page: int = 1,
        page_size: int = 12,
    ) -> dict[str, Any]:
        self._refresh_if_needed()
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 50)
        normalized_category = (category or "").strip()
        with self._lock:
            matches = [
                item
                for item in self._products
                if self._matches_query(item, query)
                and (not normalized_category or item.get("category") == normalized_category)
            ]
            total = len(matches)
            start = (page - 1) * page_size
            items = copy.deepcopy(matches[start : start + page_size])
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": math.ceil(total / page_size) if total else 0,
            "query": query,
            "category": normalized_category or None,
        }

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        self._refresh_if_needed()
        normalized = str(product_id or "").strip().upper()
        with self._lock:
            product = self._by_id.get(normalized)
            if product is None:
                # 前端也可以使用公开 alias 打开详情。
                alias_id = _local_product_id(product_id)
                product = self._by_id.get(alias_id)
            return copy.deepcopy(product) if product else None
