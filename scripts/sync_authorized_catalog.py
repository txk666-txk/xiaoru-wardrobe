#!/usr/bin/env python3
"""Build a read-only local catalogue from your authorized Youzan storefront.

This script intentionally uses only public storefront pages. It does not log in,
call merchant APIs, place orders, or claim that the store belongs to this project.

After you explicitly select your store, it collects the full public catalogue and enriches the first page with
public SKU data embedded in each mobile goods page. The outputs are suitable for
an offline demo catalogue; prices, stock, sales and policies must be treated as a
point-in-time snapshot rather than a live merchant integration.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DESKTOP_BASE = "https://detail.youzan.com"
MOBILE_BASE = "https://h5.youzan.com/v2/goods"
SOURCE_TYPE = "public_youzan_storefront_snapshot"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 XiaoruCatalogImport/1.0"
)


GOODS_CARD_RE = re.compile(
    r'<a href="/show/goods\?from_source=gbox_seo&amp;alias=([^\"]+)"[^>]*>'
    r'\s*<div class="image[^\"]*">\s*'
    r'<img src="([^\"]+)"[^>]*alt="([^\"]*)"[^>]*>\s*'
    r'</div>\s*<div class="title">(.*?)</div>\s*'
    r'<span class="price">￥([^<]+)</span>',
    re.DOTALL,
)
PAGE_RE = re.compile(r"(?:\?|&amp;)p=(\d+)")
LABEL_RE = re.compile(r'"labelName":"([^\"]+)"')


@dataclass(frozen=True)
class SnapshotOptions:
    kdt_id: int
    catalog_pages: int | None
    detail_limit: int
    timeout: float
    delay: float
    output_dir: Path


def fetch_text(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def catalogue_url(kdt_id: int, page: int) -> str:
    query = urlencode({"from_source": "gbox_seo", "kdt_id": kdt_id, "p": page})
    return f"{DESKTOP_BASE}/show/goods/newest?{query}"


def public_goods_url(alias: str) -> str:
    return (
        f"{DESKTOP_BASE}/show/goods?"
        + urlencode({"alias": alias, "from_source": "gbox_seo"})
    )


def mobile_goods_url(alias: str) -> str:
    return f"{MOBILE_BASE}/{alias}?from_source=gbox_seo"


def clean_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def normalize_image_url(url: str) -> str:
    value = html.unescape(url).strip()
    return re.sub(r"!\d+x\d+\.(?:jpg|jpeg|png|webp)$", "", value, flags=re.I)


def infer_category(title: str) -> str:
    rules = (
        (r"T恤|短袖|长袖|半袖|POLO|背心", "T恤/上衣"),
        (r"外套|夹克|羽绒|皮衣|风衣|大衣|棒球服|防晒服|防晒衣", "外套"),
        (r"卫衣|毛衣|针织|开衫", "卫衣/针织"),
        (r"裤|短裤|牛仔裤|卫裤|西裤|工装裤|休闲裤", "裤装"),
        (r"衬衫", "衬衫"),
        (r"裙|吊带", "裙装/吊带"),
        (r"鞋|靴|拖鞋", "鞋履"),
        (r"包|挎包|双肩包", "包袋"),
        (r"帽|袜|眼镜|项链|手链|戒指|腰带|围巾|手套|眼罩|耳环", "配饰"),
    )
    for pattern, category in rules:
        if re.search(pattern, title, flags=re.I):
            return category
    return "其他"


def parse_catalogue_page(
    page_html: str,
    page_number: int,
    snapshot_at: str,
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for position, match in enumerate(GOODS_CARD_RE.finditer(page_html), start=1):
        alias, image_url, alt_text, raw_title, raw_price = match.groups()
        title = clean_text(raw_title) or clean_text(alt_text)
        products.append(
            {
                "source_product_alias": alias,
                "name": title,
                "category": infer_category(title),
                "category_source": "title_keyword_inference",
                "price": float(raw_price.strip()),
                "currency": "CNY",
                "image_url": normalize_image_url(image_url),
                "public_url": public_goods_url(alias),
                "catalog_page": page_number,
                "catalog_position": position,
                "detail_enriched": False,
                "source_type": SOURCE_TYPE,
                "source_url": public_goods_url(alias),
                "snapshot_at": snapshot_at,
                "ownership_verified": False,
            }
        )
    return products


def extract_window_global(page_html: str) -> dict[str, Any]:
    marker = "window._global = "
    marker_index = page_html.find(marker)
    if marker_index < 0:
        raise ValueError("window._global was not found")

    start = marker_index + len(marker)
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(page_html)):
        char = page_html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(page_html[start : index + 1])
    raise ValueError("window._global JSON object was not balanced")


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def property_groups(sku_info: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    groups: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, str]] = {}
    for prop in sku_info.get("props") or []:
        key = str(prop.get("k_s") or "")
        values = prop.get("v") or []
        mapped_values = []
        by_id: dict[str, str] = {}
        for value in values:
            value_id = str(value.get("id"))
            name = str(value.get("name") or "")
            by_id[value_id] = name
            public_value: dict[str, Any] = {"id": value_id, "name": name}
            if value.get("imgUrl"):
                public_value["image_url"] = value["imgUrl"]
            mapped_values.append(public_value)
        lookup[key] = by_id
        groups.append(
            {
                "name": prop.get("k"),
                "key": key,
                "values": mapped_values,
            }
        )
    return groups, lookup


def public_variants(
    sku_info: dict[str, Any],
    prop_lookup: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    prices = {
        str(row.get("skuId")): float(row.get("price", 0)) / 100
        for row in (sku_info.get("skuPrices") or [])
    }
    stocks = {
        str(row.get("skuId")): {
            "stock": row.get("stockNum"),
            "disabled": bool(row.get("disable", False)),
        }
        for row in (sku_info.get("skuStocks") or [])
    }
    variants: list[dict[str, Any]] = []
    for row in sku_info.get("skus") or []:
        sku_id = str(row.get("skuId"))
        attributes: dict[str, str] = {}
        for group in sku_info.get("props") or []:
            key = str(group.get("k_s") or "")
            value_id = str(row.get(key, ""))
            value_name = prop_lookup.get(key, {}).get(value_id)
            if value_name:
                attributes[str(group.get("k") or key)] = value_name
        stock_record = stocks.get(sku_id, {})
        variant: dict[str, Any] = {
            "source_sku_id": sku_id,
            "attributes": attributes,
        }
        if sku_id in prices:
            variant["price"] = prices[sku_id]
            variant["currency"] = "CNY"
        if "stock" in stock_record and stock_record["stock"] is not None:
            variant["stock_public"] = int(stock_record["stock"])
            variant["stock_source"] = "public_embedded_sku_snapshot"
        if stock_record:
            variant["disabled_public"] = stock_record["disabled"]
        variants.append(variant)
    return variants


def category_properties(goods: dict[str, Any]) -> dict[str, Any] | None:
    model = goods.get("itemCatePropDetailModel") or {}
    categories = model.get("categoryNames") or []
    properties: dict[str, list[str]] = {}
    for row in model.get("propertiesDetailModels") or []:
        name = row.get("proName")
        values = row.get("valNames") or []
        if name:
            properties[str(name)] = [str(value) for value in values]
    if not categories and not properties:
        return None
    return {"category_path": categories, "properties": properties}


def enrich_product(product: dict[str, Any], page_html: str) -> tuple[dict[str, Any], dict[str, Any]]:
    global_data = extract_window_global(page_html)
    goods_data = global_data.get("goodsData") or {}
    goods = goods_data.get("goods") or {}
    sku_info = goods_data.get("skuInfo") or {}
    groups, lookup = property_groups(sku_info)
    variants = public_variants(sku_info, lookup)
    sku_prices = [row["price"] for row in variants if "price" in row]
    public_stocks = [row["stock_public"] for row in variants if "stock_public" in row]
    pictures = goods.get("pictures") or []

    enriched = dict(product)
    enriched.update(
        {
            "source_product_id": str(goods.get("id")) if goods.get("id") is not None else None,
            "name": first_nonempty(goods.get("title"), product.get("name")),
            "subtitle": goods.get("subTitle"),
            "selling_point": goods.get("sellPoint"),
            "price": min(sku_prices) if sku_prices else product.get("price"),
            "price_max": max(sku_prices) if sku_prices else product.get("price"),
            "original_price_public": goods.get("origin") or None,
            "sold_count_public": goods.get("soldNum"),
            "stock_total_public": sum(public_stocks) if public_stocks else None,
            "stock_source": "public_embedded_sku_snapshot" if public_stocks else None,
            "specification_groups": groups,
            "variants": variants,
            "category_properties_public": category_properties(goods),
            "support_unconditional_return_public": goods.get("supportUnconditionalReturn"),
            "support_quick_refund_public": goods.get("supportQuickRefund"),
            "support_barter_public": (goods.get("barterModel") or {}).get("isSupportBarter"),
            "sold_status_public": goods.get("soldStatus"),
            "service_labels_public": list(dict.fromkeys(LABEL_RE.findall(page_html))),
            "detail_enriched": True,
        }
    )
    if pictures:
        enriched["image_url"] = pictures[0].get("url") or enriched["image_url"]
        enriched["picture_urls"] = [
            picture.get("url") for picture in pictures if picture.get("url")
        ]
    enriched = {key: value for key, value in enriched.items() if value is not None}
    return enriched, global_data


def discover_page_count(first_page_html: str) -> int:
    page_numbers = [int(value) for value in PAGE_RE.findall(first_page_html)]
    return max(page_numbers, default=1)


def build_snapshot(options: SnapshotOptions) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot_at = datetime.now().astimezone().isoformat(timespec="seconds")
    first_page_html = fetch_text(catalogue_url(options.kdt_id, 1), options.timeout)
    page_count = options.catalog_pages or discover_page_count(first_page_html)
    products: list[dict[str, Any]] = []

    for page_number in range(1, page_count + 1):
        page_html = (
            first_page_html
            if page_number == 1
            else fetch_text(catalogue_url(options.kdt_id, page_number), options.timeout)
        )
        page_products = parse_catalogue_page(page_html, page_number, snapshot_at)
        if not page_products:
            raise RuntimeError(f"No public products parsed from catalogue page {page_number}")
        products.extend(page_products)
        if options.delay and page_number != page_count:
            time.sleep(options.delay)

    aliases = [product["source_product_alias"] for product in products]
    if len(aliases) != len(set(aliases)):
        raise RuntimeError("Duplicate product aliases were found in the public catalogue")

    detail_errors: list[dict[str, str]] = []
    first_global: dict[str, Any] | None = None
    detail_count = min(max(options.detail_limit, 0), len(products))
    for index in range(detail_count):
        alias = products[index]["source_product_alias"]
        try:
            detail_html = fetch_text(mobile_goods_url(alias), options.timeout)
            products[index], global_data = enrich_product(products[index], detail_html)
            if first_global is None:
                first_global = global_data
        except Exception as exc:  # preserve the catalogue even if one detail page drifts
            detail_errors.append({"alias": alias, "error": str(exc)})
            products[index]["detail_error"] = str(exc)
        if options.delay and index + 1 != detail_count:
            time.sleep(options.delay)

    goods_data = (first_global or {}).get("goodsData") or {}
    shop = goods_data.get("shop") or {}
    shop_meta = (first_global or {}).get("shopMetaInfo") or {}
    delivery = goods_data.get("delivery") or {}
    shop_config = goods_data.get("shopConfig") or {}

    catalog_source_url = (
        f"{DESKTOP_BASE}/show/goods/newest?"
        + urlencode({"from_source": "gbox_seo", "kdt_id": options.kdt_id})
    )
    store_profile = {
        "schema_version": 1,
        "store_name": first_nonempty(shop.get("shopName"), shop_meta.get("shopName")),
        "kdt_id": options.kdt_id,
        "shop_alias": shop.get("alias"),
        "logo_url": shop.get("logo"),
        "public_total_item_count": shop.get("totalItemCount"),
        "public_praise_user_count": shop.get("praiseUserCount"),
        "catalog_items_collected": len(products),
        "catalog_pages_collected": page_count,
        "detail_items_enriched": sum(bool(product.get("detail_enriched")) for product in products),
        "detail_errors": detail_errors,
        "payment_methods_observed_on_public_pc_page": ["微信支付", "支付宝"],
        "delivery_public_snapshot": {
            "supports_express": delivery.get("supportExpress"),
            "supports_local_delivery": delivery.get("supportLocalDelivery"),
            "supports_self_fetch": delivery.get("supportSelfFetch"),
            "delivery_address_context": delivery.get("deliveryAddress"),
            "express_fee_display": (delivery.get("express") or {}).get("desc"),
        },
        "shop_settings_public_snapshot": {
            "freight_insurance": shop_config.get("freightInsurance"),
            "estimated_arrival_prediction": shop_config.get(
                "estimatedArrivalTimePredictionSwitch"
            ),
            "web_customer_service_in_goods": shop_config.get("showWscWebIm"),
        },
        "source_type": SOURCE_TYPE,
        "source_url": catalog_source_url,
        "snapshot_at": snapshot_at,
        "ownership_verified": False,
        "ownership_notice": (
            "本文件来自无需登录即可访问的公开商品页，仅用于参考与演示；"
            "未验证项目使用者拥有或获得该店铺授权。"
        ),
        "limitations": [
            "这不是有赞商家API或后台授权接入，不能创建、修改、退款或查询真实订单。",
            "价格、库存、销量、运费和售后标签仅代表抓取时公开页面展示。",
            "商品图片和品牌素材的知识产权仍归原权利人，项目不得据此宣称品牌合作。",
            "客服时间、真实发货时效、退货地址、发票政策和售后SLA无法从公开目录确认。",
        ],
    }

    product_dataset = {
        "schema_version": 1,
        "dataset_label": "有赞公开商品页参考快照（非商家API授权）",
        "store_name": store_profile["store_name"],
        "kdt_id": options.kdt_id,
        "source_type": SOURCE_TYPE,
        "source_url": catalog_source_url,
        "snapshot_at": snapshot_at,
        "ownership_verified": False,
        "catalog_items_collected": len(products),
        "detail_items_enriched": store_profile["detail_items_enriched"],
        "products": products,
    }
    return store_profile, product_dataset


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kdt-id", type=int, required=True, help="ID of a store whose catalogue you are authorized to use; no default store is bundled.")
    parser.add_argument(
        "--catalog-pages",
        type=int,
        default=None,
        help="Pages to collect. Default: discover the public last page.",
    )
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=48,
        help="Number of leading catalogue items to enrich with public SKU data.",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--delay", type=float, default=0.08)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "agent" / "data",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    options = SnapshotOptions(
        kdt_id=args.kdt_id,
        catalog_pages=args.catalog_pages,
        detail_limit=args.detail_limit,
        timeout=args.timeout,
        delay=max(args.delay, 0.0),
        output_dir=args.output_dir.resolve(),
    )
    store_profile, product_dataset = build_snapshot(options)
    profile_path = options.output_dir / "store_profile.json"
    products_path = options.output_dir / "products_youzan_public.json"
    write_json(profile_path, store_profile)
    write_json(products_path, product_dataset)
    print(
        json.dumps(
            {
                "store_profile": str(profile_path),
                "products": str(products_path),
                "catalog_items": product_dataset["catalog_items_collected"],
                "detail_items": product_dataset["detail_items_enriched"],
                "detail_errors": len(store_profile["detail_errors"]),
                "snapshot_at": product_dataset["snapshot_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
