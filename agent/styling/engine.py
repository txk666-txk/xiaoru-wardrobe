from __future__ import annotations

import itertools
import re
import math

from agent.store.service import CommerceServiceError, get_commerce_service
from .preferences import SCENES, clean_preferences
from .vision import COLORS, STYLES, StylingError

GROUPS = {"T恤/上衣": "tops", "卫衣/针织": "tops", "衬衫": "tops", "裤装": "bottoms",
          "外套": "outerwear", "裙装/吊带": "one-pieces"}
LABELS = {"tops": "上衣", "bottoms": "裤装", "outerwear": "外套", "one-pieces": "裙装/连衣裙",
          "shoes": "鞋", "accessory": "配饰"}
NEUTRALS = {"黑", "白", "灰", "米", "棕"}
KEYWORDS = {"简约": ("纯色", "简约", "基础", "直筒"), "休闲": ("休闲", "宽松", "牛仔", "卫衣"),
            "通勤": ("衬衫", "西装", "直筒", "针织"), "运动": ("运动", "卫裤", "速干"),
            "复古": ("复古", "做旧", "皮衣", "格纹"), "街头": ("街头", "美式", "印花", "工装")}


def group_of(product):
    # The snapshot merges skirts and camisoles; distinguish them from titles.
    if product.get("category") == "裙装/吊带":
        name = product.get("name", "")
        if any(word in name for word in ("半身裙", "裙裤", "短裙", "长裙")) and "连衣裙" not in name:
            return "bottoms"
        if any(word in name for word in ("连衣裙", "连体", "套裙")):
            return "one-pieces"
        if any(word in name for word in ("吊带", "背心")):
            return "tops"
    return GROUPS.get(product.get("category"))


def normalize_styling_text(text):
    text = str(text or '')[:500]
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}
    def number(words):
        total, digit = 0, 0
        for word in words:
            if word in digits:
                digit = digits[word]
            else:
                total += (digit or 1) * {'十': 10, '百': 100, '千': 1000}[word]
                digit = 0
        return total + digit
    # Common ASR answers; convert only measurements/currency or a lone number, not product names.
    text = re.sub(r'(?:身高)?一米([五六七八九])(?![十百千])([零一二三四五六七八九])?',
                  lambda m: f"身高{100 + digits[m[1]] * 10 + (digits[m[2]] if m[2] else 0)}cm", text)
    text = re.sub(r'((?:预算|身高|体重)\s*)([一二两三四五六七八九十百千零]+)',
                  lambda m: m[1] + str(number(m[2])), text)
    text = re.sub(r'([一二两三四五六七八九十百千零]+)(元|块|公斤|千克|斤|厘米|度)',
                  lambda m: str(number(m[1])) + m[2], text)
    if re.fullmatch(r'[一二两三四五六七八九十百千零]+[。！]?', text):
        text = str(number(text.rstrip('。！')))
    return text


def parse_feedback(text, current=None):
    text = normalize_styling_text(text)
    result = dict(current or {})
    for key, values in {"scene": SCENES, "style": STYLES, "fit": ("宽松", "合身", "修身"),
                        "gender": ("男款", "女款", "中性")}.items():
        found = [value for value in values if value in text and not any(
            marker + value in text for marker in ("不要", "不喜欢", "不想", "排除"))]
        if found:
            result[key] = found[-1]
    if "太正式" in text or "不想太正式" in text:
        result["style"] = "休闲"
    if "太花哨" in text:
        result["style"] = "简约"
    for season, words in {"春季": ("春天", "春季"), "夏季": ("夏天", "夏季"),
                          "秋季": ("秋天", "秋季"), "冬季": ("冬天", "冬季")}.items():
        if any(word in text for word in words):
            result["season"] = season
    for body, words in {"肩部较宽": ("肩宽", "肩部较宽"), "腰腹较丰满": ("腰腹较丰满", "肚子", "腰腹"),
                        "胯部较宽": ("胯宽", "梨形", "胯部较宽"), "偏瘦": ("偏瘦",), "匀称": ("匀称",)}.items():
        if any(word in text for word in words) and not re.search(r"肩宽\s*\d", text):
            result["body_type"] = body
    for scene, words in {"通勤": ("上班", "办公室"), "日常": ("平时", "日常"), "面试": ("求职",),
                         "旅行": ("旅游", "出游"), "约会": ("约会",), "运动": ("健身",)}.items():
        if any(word in text for word in words):
            result["scene"] = scene
    temperature = re.search(r"(?:气温|温度)?\s*(-?\d{1,2})\s*(?:度|℃)", text)
    if temperature:
        result["temperature"] = float(temperature[1])
    from agent.router import Router
    entities = Router.extract_entities(text)
    for key in ("height_cm", "weight_kg"):
        if entities.get(key):
            result[key] = entities[key]
    match = re.search(r"(?:预算|不超过|以内|控制在)\s*[￥¥]?\s*(\d{2,5}(?:\.\d+)?)", text)
    match = match or re.search(r"(\d{2,5})\s*(?:元以内|元预算)", text)
    if match:
        result["budget"] = float(match[1])
    excluded, liked = list(result.get("excluded_colors", [])), list(result.get("colors", []))
    for color in COLORS:
        if any(word + color in text for word in ("不要", "不喜欢", "避开", "排除")):
            excluded.append(color)
            liked = [item for item in liked if item != color]
        elif any(word + color in text for word in ("喜欢", "偏好", "换成")):
            liked.append(color)
            excluded = [item for item in excluded if item != color]
    if excluded or "excluded_colors" in result:
        result["excluded_colors"] = list(dict.fromkeys(excluded))[:6]
    if liked or "colors" in result:
        result["colors"] = list(dict.fromkeys(liked))[:6]
    return clean_preferences(result)


def aesthetic_advice(observation, options=None):
    """Ground taste advice in visible clothing; do not rate or infer the person's body."""
    garments = (observation or {}).get("garments", [])
    if not garments:
        return []
    colors = list(dict.fromkeys(item.get("color") for item in garments if item.get("color") in COLORS))
    advice = []
    if colors:
        names = "、".join(color + "色" for color in colors)
        if len(set(colors) - NEUTRALS) > 2:
            advice.append(f"照片里有{names}。我建议保留一处亮色作重点，其余用黑白灰或米色收住，视觉更有主次。")
        elif set(colors).issubset(NEUTRALS):
            advice.append(f"照片里的{names}属于基础配色，组合比较柔和；可以让鞋或包呼应其中一种颜色，整套更连贯。")
        else:
            advice.append(f"照片里可见{names}。可以保留这组配色，用一件基础色单品衔接，避免再增加太多亮色。")
    patterns = [item.get("pattern") for item in garments if item.get("pattern") in {"条纹", "格纹", "印花"}]
    if patterns:
        advice.append("已有花纹可以作为视觉重点，搭配纯色单品会更容易协调。")
    if any(item.get("fit") == "宽松" for item in garments):
        advice.append("照片中有宽松单品；如果想让轮廓更利落，可以尝试宽松上衣配直线裤装，避免整套都过分蓬松。")
    if (options or {}).get("scene") in {"通勤", "面试"}:
        advice.append("按通勤需求，我会优先考虑简洁线条和较少花纹；正式程度还要结合你的工作环境。")
    if not advice:
        labels = '、'.join(dict.fromkeys(LABELS.get(item.get('category'), '服装') for item in garments))
        advice.append(f'目前能辨认出{labels}，但配色和轮廓还不够清晰。你可以补充颜色或换一张光线充足的照片，我再具体给搭配意见。')
    return advice[:3]


def colors_of(product):
    text = str(product.get("name", ""))
    for variant in product.get("variants", []):
        for key, value in variant.get("attributes", {}).items():
            if any(word in key for word in ("颜色", "配色", "色号")):
                text += " " + str(value)
    return [color for color in COLORS if color in text]


class StylingEngine:
    def __init__(self, commerce=None):
        self.commerce = commerce

    @property
    def store(self):
        return self.commerce or get_commerce_service()

    def catalog(self):
        result, page = [], 1
        while True:
            data = self.store.list_products(page=page, page_size=50)
            result.extend(data["items"])
            if page >= data.get("pages", 1):
                return result
            page += 1

    def card(self, product, options):
        sizes = []
        size_note = "尚未核对尺码，可点尺码推荐补充数据"
        if options.get("height_cm") and options.get("weight_kg"):
            try:
                size = self.store.recommend_size(product_id=product["product_id"],
                    height_cm=options["height_cm"], weight_kg=options["weight_kg"],
                    fit_preference=options.get("fit"))
                sizes = size.get("recommendations", [])
                size_note = "公开尺码初筛，不能保证合身" if sizes else "数据未落入可售尺码范围，请核对"
            except CommerceServiceError:
                size_note = "此款缺少可核验尺码资料，不猜码"
        return {"product_id": product["product_id"], "name": product["name"],
                "category": product.get("category"), "price": product.get("price"),
                "image_url": product.get("image_url"), "source_url": product.get("public_url") or product.get("source_url"),
                "snapshot_at": product.get("snapshot_at"), "colors": colors_of(product),
                "tryon_supported": bool(product.get("tryon_supported")),
                "stock_note": ("库存待核验" if product.get("stock_total_public") is None else
                               "公开快照暂时缺货" if product["stock_total_public"] == 0 else "快照中有库存"),
                "sizes": sizes, "size_note": size_note, "detail_verified": bool(product.get("detail_enriched")),
                "group": group_of(product)}

    def plan(self, options=None, observation=None, keep_ids=None, exclude_ids=None, max_item_prices=None, max_total_price=None):
        options = {"scene": "日常", "style": "简约", "budget": 300, "gender": "不限",
                   **clean_preferences({} if options is None else options)}
        if "season" in options and "temperature" not in options:
            options["temperature"] = {"春季": 18, "夏季": 28, "秋季": 16, "冬季": 8}[options["season"]]
        keep_ids, exclude_ids = set(keep_ids or []), set(exclude_ids or [])
        max_item_prices = max_item_prices or {}
        if (not isinstance(max_item_prices, dict) or any(
                group not in LABELS or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 for group, value in max_item_prices.items())):
            raise StylingError('invalid_price_limit', '单品价格上限无效')
        if max_total_price is not None and (isinstance(max_total_price, bool) or
                not isinstance(max_total_price, (int, float)) or not math.isfinite(max_total_price) or max_total_price < 0):
            raise StylingError('invalid_price_limit', '搭配价格上限无效')
        if keep_ids & exclude_ids or len(keep_ids) > 3 or len(exclude_ids) > 20:
            raise StylingError("invalid_selection", "保留与排除的商品不能冲突，最多保留三件")
        catalog = self.catalog()
        by_id = {p["product_id"]: p for p in catalog}
        if not keep_ids.issubset(by_id) or not exclude_ids.issubset(by_id):
            raise StylingError("invalid_product", "所选商品不在当前目录，请刷新后重试")
        observed = (observation or {}).get("garments", [])
        owned = observed if (observation or {}).get("mode") == "garment" else []
        # Owned items replace that category rather than recommending a duplicate purchase.
        owned_groups = {item["category"] for item in owned if item["category"] in GROUPS.values()}
        anchor_colors = {item.get("color") for item in observed if item.get("color") in COLORS}
        groups = ["tops", "bottoms"]
        if "one-pieces" in owned_groups:
            groups = []
        if options.get("temperature", 25) < 18 and "outerwear" not in groups:
            groups.append("outerwear")
        groups = [group for group in groups if group not in owned_groups]
        keep_groups = {group_of(by_id[item]) for item in keep_ids}
        if len(keep_groups) != len(keep_ids):
            raise StylingError("duplicate_category", "一套搭配每个品类只保留一件，请取消重复品类")
        groups = [group for group in groups if group not in keep_groups]
        if "one-pieces" in keep_groups:
            groups = [group for group in groups if group not in {"tops", "bottoms"}]

        def eligible(product):
            if product["product_id"] in exclude_ids or product.get("price") is None:
                return False
            if product.get("stock_total_public") == 0 or not math.isfinite(product["price"]) or product["price"] < 0:
                return False
            if product['price'] > max_item_prices.get(group_of(product), float('inf')):
                return False
            gender, name = options["gender"], product["name"]
            if gender in {"男款", "女款"}:
                from agent.tools.common import matches_requested_gender
                if not matches_requested_gender(name, gender[0]):
                    return False
            if gender == "中性" and ("男" in name or "女" in name) and "情侣" not in name and not ("男" in name and "女" in name):
                return False
            return not set(colors_of(product)) & set(options.get("excluded_colors", []))

        def score(product):
            name, colors = product["name"], set(colors_of(product))
            words = list(KEYWORDS[options["style"]])
            if options["scene"] in {"通勤", "面试"}:
                words += list(KEYWORDS["通勤"])
            if options["scene"] in {"旅行", "运动"}:
                words += list(KEYWORDS["休闲" if options["scene"] == "旅行" else "运动"])
            value = sum(3 for word in words if word in name)
            value += 3 * int(options.get("fit", "invalid") in name)
            body_words = {'肩部较宽': ('落肩', '插肩', 'V领'), '腰腹较丰满': ('直筒', '宽松', '高腰'),
                          '胯部较宽': ('直筒', '阔腿', 'A字'), '偏瘦': ('叠穿', '针织'), '匀称': ()}
            value += sum(2 for word in body_words.get(options.get('body_type'), ()) if word in name)
            value += 3 * len(colors & set(options.get("colors", [])))
            value += 2 * int(bool(colors & (anchor_colors | NEUTRALS)))
            value += int(bool(product.get("detail_enriched"))) * 2
            if options.get("temperature", 25) >= 26:
                value += 3 * int(any(w in name for w in ("短袖", "薄款", "夏季")))
                value -= 7 * int(any(w in name for w in ("羽绒", "加绒", "棉服")))
            elif options.get("temperature", 25) < 18:
                value += 3 * int(any(w in name for w in ("长袖", "针织", "秋冬", "加绒")))
                value -= 5 * int(any(w in name for w in ("短裤", "短袖", "夏季")))
            return value

        if any(not eligible(by_id[item]) or group_of(by_id[item]) is None for item in keep_ids):
            raise StylingError("unavailable_selection", "保留商品与预算偏好或公开库存冲突，请取消保留或调整条件")
        kept = [by_id[item] for item in sorted(keep_ids)]
        group_plans = [groups]
        if options["gender"] == "女款" and not owned_groups and not keep_groups:
            group_plans.append(["one-pieces"] + (["outerwear"] if options.get("temperature", 25) < 18 else []))
        pools = {}
        for group in set(g for plan in group_plans for g in plan):
            ranked = sorted([p for p in catalog if group_of(p) == group and eligible(p)
                             and p["product_id"] not in keep_ids], key=lambda p: (-score(p), p["price"], p["product_id"]))
            # Include affordable alternatives even when high relevance items are costly.
            low_price = sorted(ranked, key=lambda p: (p["price"], -score(p)))[:5]
            pool = {p["product_id"]: p for p in ranked[:10] + low_price}
            pools[group] = list(pool.values())
        combinations = []
        for plan in group_plans:
            choices = [pools[g] for g in plan]
            combinations.extend([kept + list(combo) for combo in itertools.product(*choices)] if choices else [kept])
        possible = []
        for products in combinations:
            if not products or len({p["product_id"] for p in products}) != len(products):
                continue
            total = round(sum(p["price"] for p in products), 2)
            if total <= min(options["budget"], max_total_price if max_total_price is not None else options["budget"]):
                palette = set().union(*(set(colors_of(p)) for p in products)) | anchor_colors
                harmony = 4 if palette and len(palette - NEUTRALS) <= 1 else -4 * max(0, len(palette - NEUTRALS) - 2)
                possible.append((sum(score(p) for p in products) + harmony, total, products))
        possible.sort(key=lambda row: (-row[0], row[1]))
        outfits, seen = [], set()
        for _, total, products in possible:
            ids = frozenset(p["product_id"] for p in products)
            if ids in seen or outfits and ids == frozenset(i["product_id"] for i in outfits[0]["items"]):
                continue
            seen.add(ids)
            cards = [self.card(p, options) for p in products]
            for card, product in zip(cards, products):
                hits = [word for word in KEYWORDS[options["style"]] if word in product["name"]]
                card["reason"] = ("商品标题包含“" + "、".join(hits[:2]) + "”，可作风格初筛。" if hits
                                  else f"作为{LABELS.get(card['group'], '单品')}搭配，优先考虑配色和预算。")
            reason = f"围绕{options['scene']}场景，优先筛选标题中与{options['style']}相关的款式，新增商品参考合计 ¥{total:.2f}。"
            if owned:
                reason = "保留照片中已有衣服，只补充其余品类。" + reason
            palette = set().union(*(set(card["colors"]) for card in cards))
            if palette:
                reason += "配色建议：" + "、".join(c + "色" for c in COLORS if c in palette) + "；可用鞋包呼应主色。颜色按目录资料初筛，请结合商品图片确认。"
            if options.get('body_type'):
                reason += '版型按你自述的体型偏好筛选，实际效果需结合服装尺寸和试穿确认。'
            outfits.append({"title": "推荐搭配" if not outfits else "另一种选择", "items": cards,
                            "total": total, "reason": reason, "owned_items": owned})
            if len(outfits) == 2:
                break
        summary = ("已有衣服可以继续穿，不必整套重买。" if owned else "先给你两种搭配思路，可以保留喜欢的单品再调整。")
        if not outfits and not groups and owned and not kept:
            summary = "照片中的已有衣服已覆盖这套搭配，不必额外购买；可以调整场景或气温再看看是否需要外套。"
        elif not outfits:
            summary = "当前目录未找到同时符合预算与偏好的搭配。可以调整配色或款式条件，也可以先穿已有衣服；不会推荐超预算的方案。"
        elif len(outfits) == 1:
            summary = "当前条件下找到一套搭配，可以调整预算或风格再看看其他选择。"
        return {"options": options, "outfits": outfits, "summary": summary,
                "aesthetic_advice": aesthetic_advice(observation, options),
                "observation": observation, "steps": ["确认需求", "检索商品", "预算与偏好筛选", "搭配规划", "核对价格与尺码"],
                "notice": "搭配是审美建议；价格、库存与尺码请核对目录说明。照片不用于推算身体数据，生成图不等于真实合身效果。"}

    def compare(self, ids, options=None):
        if (not isinstance(ids, list) or not 2 <= len(ids) <= 3 or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids)):
            raise StylingError("invalid_comparison", "请选择两到三件不同商品进行比较")
        cards = [self.card(self.store.get_product(item), clean_preferences({} if options is None else options)) for item in ids]
        cheapest = min(cards, key=lambda p: p.get("price") if p.get("price") is not None else float("inf"))
        summary = (f"如果优先考虑预算，可以先看 {cheapest['name'][:28]}，参考价 ¥{cheapest['price']:.2f}。"
                   if cheapest.get("price") is not None else "这几件暂无可核验价格，请先查看商品原页。")
        return {"items": cards, "summary": summary + "已有相近款时可以不买；未核实面料、耐用性或舒适度，不据此评优。"}
