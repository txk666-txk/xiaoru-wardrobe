"""电商客服请求路由。

Stage 1 使用确定性规则完成首层分流，避免把所有请求都送进复杂推理循环。
规则无法可靠判断时返回 ``general``，由 Fay 原有 LLM 链路继续处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteResult:
    intent: str
    strategy: str
    confidence: float
    entities: dict[str, str]


_ORDER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])((?:(?:DEMO|FX)-\d{8}-[A-Fa-f0-9]{8})|(?:[A-Za-z]\d{5,}))(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PRODUCT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(EC\d{4,}|YZREF-[A-Za-z0-9]+)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_HEIGHT_PATTERN = re.compile(r"(?:身高\s*(?:是|为|[:：])?\s*)?(\d{3}(?:\.\d+)?)\s*(?:cm|厘米)", re.IGNORECASE)
_LABELED_HEIGHT_PATTERN = re.compile(r"身高\s*(?:是|为|[:：])?\s*(\d{3}(?:\.\d+)?)(?!\d)", re.IGNORECASE)
_WEIGHT_PATTERN = re.compile(
    r"(?:体重\s*(?:是|为|[:：])?\s*)?(\d{2,3}(?:\.\d+)?)\s*(kg|公斤|千克|斤)",
    re.IGNORECASE,
)
_LABELED_WEIGHT_PATTERN = re.compile(
    r"体重\s*(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(kg|公斤|千克|斤)?(?!\d)",
    re.IGNORECASE,
)
_SLASH_MEASUREMENT_PATTERN = re.compile(r"(?<!\d)(\d{3}(?:\.\d+)?)\s*[/／]\s*(\d{2,3}(?:\.\d+)?)(?!\d)")
_OPTIONAL_MEASUREMENTS = {
    "chest_cm": re.compile(r"胸围\s*(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米)?", re.IGNORECASE),
    "shoulder_cm": re.compile(r"肩宽\s*(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米)?", re.IGNORECASE),
    "waist_cm": re.compile(r"腰围\s*(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米)?", re.IGNORECASE),
    "hip_cm": re.compile(r"臀围\s*(?:是|为|[:：])?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米)?", re.IGNORECASE),
}


class Router:
    """把用户问题分为商品、订单、物流、政策和售后等意图。"""

    _high_risk_phrases = (
        "帮我退款", "给我退款", "申请退款", "处理退款", "立即退款",
        "帮我退货", "申请退货", "处理退货", "取消订单", "帮我取消",
        "修改地址", "更改地址", "改收货地址",
    )
    _consultation_markers = (
        "如何", "怎么", "怎样", "需要什么", "什么条件", "流程", "可以吗",
        "能否", "能不能", "怎么办", "规则", "政策",
    )
    _after_sales_words = (
        "坏了", "破损", "损坏", "故障", "瑕疵", "质量问题", "少件",
        "漏发", "错发", "售后", "退货", "退款", "换货", "维修",
    )
    _logistics_words = (
        "物流", "快递", "到哪", "到哪里", "送到", "到货", "配送",
        "运输", "签收", "预计到", "什么时候到", "还没到",
    )
    _order_words = (
        "订单", "下单", "购买记录", "付款", "支付状态", "订单状态",
        "买了什么", "购买时间",
    )
    _product_words = (
        "商品", "产品", "多少钱", "价格", "库存", "有货", "缺货",
        "尺码", "尺寸", "颜色", "规格", "材质", "参数", "适合",
        "有哪些", "推荐", "保温杯", "耳机", "托特包", "西装",
        "T恤", "短袖", "长袖", "衬衫", "夹克", "外套", "羽绒服",
        "卫衣", "毛衣", "针织", "裤", "牛仔", "裙", "吊带", "包袋",
    )
    _policy_words = (
        "七天无理由", "7天无理由", "退换货政策", "退款政策", "保修",
        "运费谁出", "优惠券", "发票", "隐私", "个人信息", "活动规则",
        "退款多久到账", "退款时间", "售后规则",
    )
    _large_return_words = ("大型订单", "大额订单", "大单", "大宗", "批量退货", "大量退货")
    _shopping_words = ("想买", "买件", "买点", "想找", "帮我挑", "帮我选")
    _styling_markers = ("穿搭", "搭配", "搭一套", "搭一身", "配一套", "配一身")

    @staticmethod
    def _contains_any(text: str, words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    @staticmethod
    def extract_entities(text: str) -> dict[str, str]:
        entities: dict[str, str] = {}
        order_match = _ORDER_PATTERN.search(text)
        if order_match:
            entities["order_id"] = order_match.group(1).upper()
        product_match = _PRODUCT_PATTERN.search(text)
        if product_match:
            entities["product_id"] = product_match.group(1).upper()
        height_match = _HEIGHT_PATTERN.search(text) or _LABELED_HEIGHT_PATTERN.search(text)
        if height_match:
            entities["height_cm"] = height_match.group(1)
        weight_match = _WEIGHT_PATTERN.search(text) or _LABELED_WEIGHT_PATTERN.search(text)
        if weight_match:
            weight = float(weight_match.group(1))
            if (weight_match.group(2) or "").lower() == "斤":
                weight /= 2
            entities["weight_kg"] = f"{weight:g}"
        if not height_match or not weight_match:
            slash_match = _SLASH_MEASUREMENT_PATTERN.search(text)
            if slash_match:
                entities.setdefault("height_cm", slash_match.group(1))
                entities.setdefault("weight_kg", slash_match.group(2))
        for key, pattern in _OPTIONAL_MEASUREMENTS.items():
            match = pattern.search(text)
            if match:
                entities[key] = match.group(1)
        if "修身" in text:
            entities["fit_preference"] = "修身"
        elif "宽松" in text:
            entities["fit_preference"] = "宽松"
        return entities

    def route(self, text: str) -> RouteResult:
        normalized = (text or "").strip()
        entities = self.extract_entities(normalized)

        if not normalized:
            return RouteResult("general", "fallback", 0.0, entities)

        if self._contains_any(normalized, self._high_risk_phrases):
            if self._contains_any(normalized, self._consultation_markers):
                return RouteResult("policy_query", "rag", 0.95, entities)
            return RouteResult("after_sales_action", "plan_and_solve", 0.98, entities)

        if (
            entities.get("height_cm")
            and entities.get("weight_kg")
            and not self._contains_any(
                normalized,
                self._order_words + self._logistics_words + self._after_sales_words,
            )
            and not self._contains_any(normalized, self._styling_markers)
        ):
            return RouteResult("size_recommendation", "tool", 0.98, entities)

        has_after_sales = self._contains_any(normalized, self._after_sales_words)
        if (
            has_after_sales
            and not entities.get("order_id")
            and self._contains_any(normalized, self._large_return_words)
        ):
            return RouteResult("policy_query", "rag", 0.96, entities)
        if has_after_sales and any(word in normalized for word in ("超过", "已经", "还能", "能不能", "怎么办")):
            return RouteResult("after_sales", "plan_and_solve", 0.94, entities)

        if has_after_sales and entities.get("order_id"):
            return RouteResult("after_sales", "plan_and_solve", 0.94, entities)

        if self._contains_any(normalized, self._logistics_words):
            return RouteResult("logistics_query", "react", 0.96, entities)

        if self._contains_any(normalized, self._order_words) or "order" in normalized.lower():
            return RouteResult("order_query", "tool", 0.92, entities)

        if self._contains_any(normalized, self._policy_words):
            return RouteResult("policy_query", "rag", 0.94, entities)

        if has_after_sales:
            return RouteResult("after_sales", "plan_and_solve", 0.90, entities)

        if self._contains_any(normalized, self._styling_markers) or any(word in normalized for word in ("保留裤子", "保留上衣", "保留外套", "太正式", "太花哨", "穿什么", "怎么穿", "穿衣建议", "这身好看", "照片穿着")) or (
            any(word in normalized for word in ("比较", "对比", "哪件更", "哪个更"))
            and len(_PRODUCT_PATTERN.findall(normalized)) >= 2
        ):
            return RouteResult("styling", "plan_and_solve", 0.96, entities)

        if entities.get("product_id"):
            return RouteResult("product_query", "tool", 0.98, entities)

        if any(word in normalized for word in ("不想买", "先不买", "暂时不买")) and not any(
            word in normalized for word in ("推荐别的", "换一款", "换个商品")
        ):
            return RouteResult("general", "fallback", 0.90, entities)

        if (
            self._contains_any(normalized, self._product_words)
            or self._contains_any(normalized, self._shopping_words)
            or "product" in normalized.lower()
        ):
            return RouteResult("product_query", "tool", 0.90, entities)

        return RouteResult("general", "fallback", 0.35, entities)
