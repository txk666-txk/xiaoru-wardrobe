from __future__ import annotations

import json
import re

from agent.providers import DemoCatalogProvider, create_default_product_provider

from .common import load_json, matches_requested_gender, requested_gender, tool_result


class ProductTool:
    name = "query_product"
    description = "查询当前配置商品目录中的名称、价格、库存和规格"

    def __init__(self) -> None:
        payload = load_json("products.json")
        self.dataset_label = payload.get("dataset_label", "演示商品数据")
        self.products = payload.get("products", [])
        try:
            self.public_provider = create_default_product_provider()
        except (OSError, ValueError, TypeError):
            # 本地目录不可用时保留原创样例，避免影响客服基础链路。
            self.public_provider = DemoCatalogProvider()
        if isinstance(self.public_provider, DemoCatalogProvider):
            # EC IDs keep the legacy response shape, but their names/prices
            # must come from the same dataset that the storefront displays.
            payload = json.loads(self.public_provider.products_path.read_text(encoding="utf-8"))
            self.dataset_label = payload.get("dataset_label", "沙盒演示商品")
            self.products = payload.get("products", [])

    @staticmethod
    def _public_query(text: str) -> str:
        query = (text or "").strip()
        for phrase in (
            "帮我看看", "帮我查", "我想买", "有没有", "有推荐的", "推荐一下",
            "商品列表", "产品列表", "多少钱", "价格", "库存", "有货吗", "有货",
            "什么尺码", "尺码", "颜色", "规格", "适合吗", "查询", "请问", "吗",
        ):
            query = query.replace(phrase, "")
        query = re.sub(r"(?i)(?:XXXL|XXL|XL|XS|S|M|L)\s*码?", "", query)
        query = re.sub(r"[？?！!，,。\s]+", "", query)
        return query

    @staticmethod
    def _explicit_alias(query: str) -> str | None:
        match = re.search(r"(?:alias[=：:]?\s*)?([a-z0-9]{12,20})", query, re.IGNORECASE)
        return match.group(1) if match else None

    @staticmethod
    def _requested_public_variants(
        query: str,
        variants: list[dict],
    ) -> tuple[list[dict], dict[str, set[str]]]:
        query_lower = query.lower()
        requested: dict[str, set[str]] = {}
        for variant in variants:
            for key, value in (variant.get("attributes") or {}).items():
                value_text = str(value).lower()
                matched = value_text in query_lower
                if not matched and "尺码" in str(key):
                    size = value_text.split()[0]
                    matched = bool(
                        re.search(rf"(?<![a-z0-9]){re.escape(size)}(?:码)?(?![a-z0-9])", query_lower)
                    )
                if matched:
                    requested.setdefault(str(key), set()).add(str(value))
        if not requested:
            return [], {}
        matches = [
            variant
            for variant in variants
            if all((variant.get("attributes") or {}).get(key) in values for key, values in requested.items())
        ]
        return matches, requested

    @staticmethod
    def _short_name(name: str, limit: int = 30) -> str:
        value = str(name or "商品").strip()
        return value if len(value) <= limit else value[:limit] + "…"

    @staticmethod
    def _display_attribute(key: str, value: object) -> str:
        text = str(value)
        if "尺码" in key and " " in text:
            size, note = text.split(" ", 1)
            return f"{size}（{note}）"
        return text

    @classmethod
    def _variant_summary(
        cls,
        variants: list[dict],
        focus: str | None = None,
    ) -> tuple[str, list[str]]:
        grouped: dict[str, dict[str, int | None]] = {}
        for variant in variants:
            for key, value in (variant.get("attributes") or {}).items():
                if focus == "颜色" and not any(word in str(key) for word in ("颜色", "配色", "色号")):
                    continue
                if focus == "尺码" and not any(word in str(key) for word in ("尺码", "尺寸", "大小")):
                    continue
                values = grouped.setdefault(str(key), {})
                stock = variant.get("stock_public")
                previous = values.get(str(value))
                if stock is None or previous is None and str(value) not in values:
                    values.setdefault(str(value), None if stock is None else int(stock))
                elif previous is not None:
                    values[str(value)] = int(previous) + int(stock)

        parts = []
        unavailable = []
        for key, values in grouped.items():
            available_values = [
                cls._display_attribute(key, value)
                for value, stock in values.items()
                if stock is None or stock > 0
            ]
            unavailable.extend(
                cls._display_attribute(key, value)
                for value, stock in values.items()
                if stock == 0
            )
            if available_values:
                shown = available_values[:8]
                suffix = "等" if len(available_values) > len(shown) else ""
                parts.append(f"{key}：{'、'.join(shown)}{suffix}")
        return "；".join(parts), unavailable

    @classmethod
    def _matching_variant_text(cls, variants: list[dict]) -> str:
        items = []
        for variant in variants[:4]:
            label = " / ".join(
                cls._display_attribute(str(key), value)
                for key, value in (variant.get("attributes") or {}).items()
            )
            stock = variant.get("stock_public")
            if stock is None:
                stock_text = "库存待核验"
            elif int(stock) > 0:
                stock_text = f"有货（{stock}件）"
            else:
                stock_text = "暂时缺货"
            items.append(f"{label}：{stock_text}")
        if len(variants) > 4:
            items.append("其余匹配规格可继续告诉我偏好后核对")
        return "；".join(items)

    def _catalog_notice(self) -> str:
        if isinstance(self.public_provider, DemoCatalogProvider):
            if any(item.get("image_attribution") for item in self.products):
                return "服装照片为开放许可实拍；价格、库存和尺码为沙盒演示参数，不代表真实在售。"
            return "以上为虚构样例，不代表真实在售。"
        profile = self.public_provider.get_store_profile()
        reference_kind = " ".join(str(profile.get(field) or "") for field in ("source_type", "dataset_label")).lower()
        if not profile.get("source_url") and any(
            word in reference_kind for word in ("local", "reference", "本机", "本地", "参考", "快照")
        ):
            return "本机参考快照；交易仅在沙盒模拟，库存待核验，不代表真实在售。"
        return "价格与库存以商品页实时展示为准。"

    def _public_recommendations(self, query: str) -> dict | None:
        if self.public_provider is None or not any(
            word in query for word in ("推荐", "挑一", "选一", "选几", "想买", "买件", "想找", "帮我挑", "帮我选")
        ):
            return None
        if any(word in query for word in ("有哪些商品", "商品列表", "产品列表", "卖什么")):
            return None

        category_words = (
            ("裤", "裤装"), ("裙", "裙装/吊带"), ("衬衫", "衬衫"),
            ("卫衣", "卫衣/针织"), ("毛衣", "卫衣/针织"),
            ("外套", "外套"), ("夹克", "外套"), ("羽绒服", "外套"),
            ("T恤", "T恤/上衣"), ("短袖", "T恤/上衣"),
            ("包", "包袋"), ("鞋", "鞋履"),
        )
        categories = {category for word, category in category_words if word in query}
        if "衣服" in query or "上衣" in query:
            categories.update(("T恤/上衣", "外套", "卫衣/针织", "衬衫"))
        budget_match = re.search(r"预算\s*(?:在|约|是|不超过)?\s*[¥￥]?\s*(\d{2,5})", query)
        if not budget_match:
            budget_match = re.search(r"[¥￥]?\s*(\d{2,5})\s*元?\s*(?:以内|以下)", query)
        budget = int(budget_match.group(1)) if budget_match else None
        gender = requested_gender(query)
        preferences = [
            word for word in (
                "春", "夏", "秋", "冬", "宽松", "修身", "复古", "美式",
                "通勤", "运动", "防晒", "羽绒", "牛仔", "皮衣", "黑色", "白色",
            ) if word in query
        ]

        if not categories and budget is None and not preferences:
            return tool_result(
                ok=True,
                message="我可以帮你挑合适的商品。你想先看上衣、裤装还是配饰？有预算也可以告诉我。",
                data={"items": []},
            )

        # A bounded catalog scan keeps recommendations local and reproducible.
        candidates = []
        for page in range(1, 41):
            result = self.public_provider.search_products(page=page, page_size=50)
            if not result["items"]:
                break
            for product in result["items"]:
                if categories and product.get("category") not in categories:
                    continue
                if not matches_requested_gender(product.get("name", ""), gender):
                    continue
                price = product.get("price")
                if price is None or budget is not None and price > budget:
                    continue
                name = str(product.get("name") or "")
                matched = [word for word in preferences if word in name]
                score = len(matched) * 10 + int(bool(product.get("detail_enriched"))) * 2
                score += int(bool(product.get("size_chart_verified")))
                candidates.append((score, product, matched))
            if page >= result["pages"]:
                break

        candidates.sort(key=lambda entry: entry[0], reverse=True)
        selected = candidates[:2]
        if not selected:
            return tool_result(
                ok=False,
                error="no_verified_recommendations",
                message="目前没有找到符合这些条件且有明确款式标注的商品。可以调整预算或品类，我再帮你筛选。",
            )
        needs_empathy = any(word in query for word in ("心情不好", "心情很差", "难过", "焦虑", "失望"))
        lines = ["听起来最近不太顺心。按你的需求，先看看：" if needs_empathy else "根据你的需求，可以先看看："]
        for _, product, matched in selected:
            reason = f"名称标注{'、'.join(matched[:2])}" if matched else f"属于{product.get('category', '相关')}品类"
            lines.append(f"- {self._short_name(product['name'], 34)}，参考价￥{product['price']:.2f}（{reason}）。")
        lines.append(self._catalog_notice() + "你更偏好哪一款？")
        return tool_result(ok=True, message="\n".join(lines), data={"items": [item[1] for item in selected]})

    def _public_product_result(self, query: str, product_id: str | None) -> dict | None:
        if self.public_provider is None:
            return None

        product = None
        if product_id:
            product = self.public_provider.get_product(product_id)
        if product is None:
            alias = self._explicit_alias(query)
            if alias:
                product = self.public_provider.get_product(alias)

        if product is None and not product_id:
            recommendations = self._public_recommendations(query)
            if recommendations is not None:
                return recommendations

        wants_catalog = any(word in query for word in ("有哪些", "商品列表", "产品列表", "卖什么", "推荐"))
        clean_query = self._public_query(query)
        search = None
        if product is None and clean_query:
            search = self.public_provider.search_products(query=clean_query, page=1, page_size=5)
            if search["items"]:
                product = search["items"][0]

        if wants_catalog and product is None:
            if search is None:
                search = self.public_provider.search_products(query=clean_query, page=1, page_size=5)
            if not search["items"]:
                search = self.public_provider.search_products(page=1, page_size=5)
            if search["items"]:
                lines = [
                    f"- {self._short_name(item['name'], 24)}：￥{item['price']:.2f}"
                    for item in search["items"][:3]
                    if item.get("price") is not None
                ]
                return tool_result(
                    ok=True,
                    message="为你找到这些商品：\n" + "\n".join(lines) + "\n" + self._catalog_notice(),
                    data=search,
                )

        if product is None:
            return None

        variants = product.get("variants") or []
        option_matches, requested_attributes = self._requested_public_variants(query, variants)
        asks_color = any(word in query for word in ("颜色", "配色", "色号"))
        asks_size = any(word in query for word in ("尺码", "尺寸", "大小"))
        asks_stock = any(word in query for word in ("库存", "有货", "缺货", "还剩"))
        if asks_color != asks_size and not asks_stock and (not requested_attributes or option_matches):
            focus = "颜色" if asks_color else "尺码"
            summary, unavailable = self._variant_summary(option_matches or variants, focus=focus)
            if summary:
                lines = [summary + "。"]
            elif unavailable:
                lines = [f"这款目前没有确认可售的{focus}。"]
            else:
                lines = [f"这款公开商品快照暂无已核实的{focus}选项。"]
            if unavailable:
                lines.append("暂缺：" + "、".join(unavailable[:6]) + "。")
            lines.append(self._catalog_notice())
            return tool_result(ok=True, message="\n".join(lines), data=product)

        price_text = "价格待核验"
        if product.get("price") is not None:
            price_text = f"参考价 ￥{product['price']:.2f}"
            if product.get("price_max") not in (None, product.get("price")):
                price_text += f"–￥{product['price_max']:.2f}"

        stock_values = [variant.get("stock_public") for variant in variants] if variants else [product.get("stock_total_public")]
        if any(stock is not None and int(stock) > 0 for stock in stock_values):
            stock_text = "目前有货"
        elif any(stock is None for stock in stock_values):
            stock_text = "库存待核验"
        else:
            stock_text = "当前未显示可售库存"
        headline = f"这款「{self._short_name(product['name'])}」{price_text}"
        if not requested_attributes:
            headline += f"，{stock_text}"
        lines = [headline + "。"]

        if option_matches:
            if len(requested_attributes) == 1 and len(option_matches) > 1:
                matching_summary, unavailable = self._variant_summary(option_matches)
                line = "你问的范围：" + matching_summary
                if unavailable:
                    line += "；暂缺 " + "、".join(unavailable[:4])
                lines.append(line + "。")
            else:
                lines.append("你问的规格：" + self._matching_variant_text(option_matches) + "。")
        elif requested_attributes:
            lines.append("当前没有找到完全匹配的颜色/尺码组合，建议换一个规格或查看商品页。")
        elif variants:
            summary, unavailable = self._variant_summary(variants)
            if summary:
                lines.append("可选" + summary + "。")
            if any(word in query for word in ("库存", "有货", "尺码", "颜色", "规格")):
                if unavailable:
                    lines.append("暂缺：" + "、".join(unavailable[:6]) + "；告诉我具体颜色和尺码可继续核对。")
                else:
                    lines.append("告诉我具体颜色和尺码，我可以继续核对对应库存。")
        else:
            lines.append("该商品暂无已核实的规格与尺码信息，请补充规格资料后再下单。")

        if len(lines) < 3 and not requested_attributes:
            lines.append("如果你告诉我用途、颜色偏好或身高体重，我可以继续帮你筛选。")
        lines = lines[:3]
        lines.append(self._catalog_notice())
        return tool_result(ok=True, message="\n".join(lines), data=product)

    @staticmethod
    def _score(product: dict, query: str, product_id: str | None) -> int:
        if product_id and product.get("product_id", "").upper() == product_id.upper():
            return 100
        score = 0
        query_lower = query.lower()
        for token in [product.get("name", ""), product.get("product_id", ""), *product.get("aliases", [])]:
            token = str(token).strip().lower()
            if token and token in query_lower:
                score += 10 + len(token)
        return score

    @staticmethod
    def _requested_options(query: str, product: dict) -> list[tuple[str, str, int]]:
        matches: list[tuple[str, str, int]] = []
        for sku in product.get("skus", []):
            attrs = sku.get("attributes", {})
            if attrs and all(str(value).lower() in query.lower() for value in attrs.values()):
                label = "、".join(f"{key}{value}" for key, value in attrs.items())
                matches.append((sku.get("sku_id", ""), label, int(sku.get("stock", 0))))
        return matches

    def execute(self, *, query: str = "", product_id: str | None = None, **_: object) -> dict:
        query = (query or "").strip()

        # Only a selected demo catalog may use the legacy EC response shape.
        # Local reference catalogs must never resolve IDs against old fixtures.
        if not (isinstance(self.public_provider, DemoCatalogProvider) and product_id and product_id.upper().startswith("EC")):
            public_result = self._public_product_result(query, product_id)
            if public_result is not None:
                return public_result
            if self.public_provider is not None and not isinstance(self.public_provider, DemoCatalogProvider):
                return tool_result(
                    ok=False,
                    error="public_product_not_found",
                    message=(
                        "我暂时没有在当前商品目录中找到对应商品。"
                        "请提供更完整的商品名称、商品编号或已配置的商品链接。"
                    ),
                )

        scored = sorted(
            ((self._score(item, query, product_id), item) for item in self.products),
            key=lambda pair: pair[0],
            reverse=True,
        )

        wants_catalog = any(word in query for word in ("有哪些", "商品列表", "产品列表", "卖什么", "推荐"))
        if wants_catalog and (not scored or scored[0][0] == 0):
            lines = [f"{item['product_id']} {item['name']}，￥{item['price']:.2f}" for item in self.products]
            return tool_result(
                ok=True,
                message=f"当前{self.dataset_label}包含：" + "；".join(lines) + "。",
                data=self.products,
            )

        if not scored or scored[0][0] <= 0:
            return tool_result(
                ok=False,
                error="product_not_found",
                message="我暂时没有找到对应商品。请提供商品名称或商品编号，例如 EC1001。",
            )

        product = scored[0][1]
        stock_total = sum(int(sku.get("stock", 0)) for sku in product.get("skus", []))
        option_matches = self._requested_options(query, product)

        details = [
            f"当前{self.dataset_label}中，{product['name']}（{product['product_id']}）售价 ￥{product['price']:.2f}",
            f"总库存 {stock_total} 件",
            f"主要规格：{'；'.join(product.get('highlights', []))}",
        ]
        if option_matches:
            option_text = "；".join(f"{label}库存 {stock} 件" for _, label, stock in option_matches)
            details.append(option_text)
        elif product.get("skus") and any(word in query for word in ("库存", "有货", "尺码", "颜色")):
            sku_lines = []
            for sku in product["skus"]:
                label = "、".join(f"{key}{value}" for key, value in sku.get("attributes", {}).items())
                sku_lines.append(f"{label} {sku.get('stock', 0)}件")
            details.append("分规格库存：" + "；".join(sku_lines))

        return tool_result(ok=True, message="，".join(details) + "。", data=product)
