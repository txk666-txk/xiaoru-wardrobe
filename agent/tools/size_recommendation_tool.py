from __future__ import annotations

from agent.store.service import CommerceServiceError, get_commerce_service

from .common import matches_requested_gender, requested_gender, tool_result


class SizeRecommendationTool:
    name = "recommend_size"
    description = "依据当前目录记录的专属尺码/体重范围，为指定单品初筛或推荐服饰"

    _category_groups = (
        ("T恤/上衣", "卫衣/针织", "衬衫"),
        ("裤装",),
        ("外套",),
        ("裙装/吊带",),
    )
    _category_keywords = (
        ("T恤", "t恤", "短袖", "长袖", "衬衫", "卫衣", "毛衣", "上衣", "针织"),
        ("裤", "牛仔", "阔腿"),
        ("外套", "夹克", "羽绒", "大衣", "防晒衣", "西装"),
        ("裙", "吊带"),
    )

    @classmethod
    def _requested_groups(cls, query: str) -> list[int]:
        selected = [
            index for index, keywords in enumerate(cls._category_keywords)
            if any(word in query for word in keywords)
        ]
        if "衣服" in query and 0 not in selected and 2 not in selected:
            selected.insert(0, 0)
            selected.append(2)
        return selected or [0, 1, 2]

    @staticmethod
    def _short_name(name: str) -> str:
        return name if len(name) <= 40 else name[:40] + "…"

    def _recommend_from_catalog(self, *, query: str, measurements: dict) -> dict:
        service = get_commerce_service()
        try:
            service._number(measurements.get("height_cm"), "身高", 120, 220)
            service._number(measurements.get("weight_kg"), "体重", 30, 200)
        except CommerceServiceError as exc:
            return tool_result(ok=False, error=exc.code, message=str(exc))

        groups = self._requested_groups(query)
        gender = requested_gender(query)
        candidates: dict[int, list[tuple[int, dict, dict]]] = {index: [] for index in groups}
        page = 1
        while True:
            catalog = service.list_products(page=page, page_size=50)
            for product in catalog["items"]:
                group = next(
                    (index for index in groups if product.get("category") in self._category_groups[index]),
                    None,
                )
                if group is None or not product.get("size_chart_verified"):
                    continue
                if not matches_requested_gender(product["name"], gender):
                    continue
                requested_types = [word for word in self._category_keywords[group] if word in query]
                if requested_types and not any(word.casefold() in product["name"].casefold() for word in requested_types):
                    continue
                try:
                    size = service.recommend_size(product_id=product["product_id"], **measurements)
                except CommerceServiceError as exc:
                    if exc.code == "size_chart_unavailable":
                        continue
                    return tool_result(ok=False, error=exc.code, message=str(exc))
                if not size.get("recommendations"):
                    continue
                name = product["name"]
                style = next((word for word in ("直筒", "宽松", "阔腿") if word in name), None)
                score = int(bool(style) and any(word in query for word in ("不显胖", "显瘦", "宽松"))) * 3
                score += sum(int(word in name) for word in self._category_keywords[group] if word in query)
                if "男" not in query and "女" not in query:
                    if "情侣" in name or ("男" in name and "女" in name):
                        score += 2
                    elif "男" in name or "女" in name:
                        score -= 1
                candidates[group].append((score, product, size))
            if page >= catalog["pages"]:
                break
            page += 1

        selected: list[tuple[dict, dict]] = []
        for group in groups:
            ranked = sorted(candidates[group], key=lambda item: item[0], reverse=True)
            if ranked:
                selected.append((ranked[0][1], ranked[0][2]))
            if len(selected) == 2:
                break
        if len(selected) < 2 and len(groups) == 1:
            for _, product, size in sorted(candidates[groups[0]], key=lambda item: item[0], reverse=True)[1:2]:
                selected.append((product, size))

        if not selected:
            preference = f"符合你所说的{gender}款、" if gender else ""
            return tool_result(
                ok=True,
                message=(
                    f"目前商品目录里没有找到{preference}能按已记录尺码范围初筛的服饰，"
                    "我不想随意猜码。你可以告诉我想看的品类，或选一件商品让我核对它的尺码表。"
                ),
                data={"recommendations": []},
            )

        demo_only = all(size.get("basis") == "fictional_demo_size_chart" for _, size in selected)
        basis_label = "样例尺码表" if demo_only else "该款已记录信息"
        lines = [f"可以先看看这两种搭配方向，尺码只按{basis_label}初筛：" if len(selected) == 2
                 else f"可以先看看这款，尺码只按{basis_label}初筛："]
        recommendations = []
        for product, size in selected:
            sizes = "、".join(size["recommendations"])
            is_demo = size.get("basis") == "fictional_demo_size_chart"
            price_label = "演示价" if is_demo else "目录参考价"
            price = f"，{price_label} ￥{product['price']:.2f}" if product.get("price") is not None else ""
            style = next((word for word in ("直筒", "宽松", "阔腿") if word in product["name"]), None)
            style_note = f"，标题标注“{style}”" if style else ""
            url = product.get("public_url") or product.get("source_url")
            source_label = "来源页" if product.get("image_attribution") else "商品页"
            link = f"；{source_label}：{url}" if isinstance(url, str) and url.startswith("https://") else ""
            overlap = "（两码区间重叠，请结合版型偏好核对成衣尺寸）" if len(size["recommendations"]) > 1 else ""
            lines.append(
                f"- {self._short_name(product['name'])}（{product['product_id']}）{price}{style_note}；"
                f"{'样例' if is_demo else '该款公开'}尺码初筛：{sizes} 码{overlap}{link}"
            )
            recommendations.append({
                "product_id": product["product_id"],
                "product_name": product["name"],
                "category": product["category"],
                "sizes": size["recommendations"],
                "source": size.get("source"),
                "public_url": url,
            })
        next_question = (
            "你喜欢宽松还是合身版型？" if gender else "你更偏好男款、女款还是中性版型？"
        )
        profile = service.provider.get_store_profile()
        reference_kind = " ".join(str(profile.get(field) or "") for field in ("source_type", "dataset_label")).lower()
        local_reference = not profile.get("source_url") and any(
            word in reference_kind for word in ("local", "reference", "本机", "本地", "参考", "快照")
        )
        if demo_only:
            notice = "以上价格、库存与尺码为沙盒演示参数，尺码范围不适用于真实购买。"
        elif local_reference:
            notice = "本机参考快照；交易仅在沙盒模拟，库存待核验。尺码仅按已记录信息初筛，不能保证合身。"
        else:
            notice = "合身度无法保证，价格、尺码和库存请以商品页实时展示为准。"
        lines.append(next_question + notice)
        return tool_result(ok=True, message="\n".join(lines), data={"recommendations": recommendations})

    def execute(
        self,
        *,
        product_id: str | None = None,
        height_cm: object = None,
        weight_kg: object = None,
        chest_cm: object = None,
        shoulder_cm: object = None,
        waist_cm: object = None,
        hip_cm: object = None,
        fit_preference: str | None = None,
        query: str = "",
        **_: object,
    ) -> dict:
        if not product_id:
            return self._recommend_from_catalog(
                query=query,
                measurements={
                    "height_cm": height_cm,
                    "weight_kg": weight_kg,
                    "chest_cm": chest_cm,
                    "shoulder_cm": shoulder_cm,
                    "waist_cm": waist_cm,
                    "hip_cm": hip_cm,
                    "fit_preference": fit_preference,
                },
            )
        try:
            data = get_commerce_service().recommend_size(
                product_id=product_id,
                height_cm=height_cm,
                weight_kg=weight_kg,
                chest_cm=chest_cm,
                shoulder_cm=shoulder_cm,
                waist_cm=waist_cm,
                hip_cm=hip_cm,
                fit_preference=fit_preference,
            )
        except CommerceServiceError as exc:
            return tool_result(ok=False, error=exc.code, message=str(exc))

        sizes = data.get("recommendations") or []
        if data.get("basis") == "fictional_demo_size_chart":
            suggestion = f"样例尺码候选：{'、'.join(sizes)} 码。" if sizes else "样例尺码表中暂无匹配区间。"
            message = suggestion + "这是虚构商品的交互演示，不能用于真实购买或保证合身。"
        elif not sizes:
            message = (
                "这组数据没有落入该款当前可售尺码范围。\n"
                "建议补充胸围、腰围等关键围度，或转人工按商品尺码表核对；线上建议不能代替试穿。"
            )
        else:
            confidence = {"high": "较高", "medium": "中等", "low": "较低"}.get(
                data.get("confidence"), "中等"
            )
            message = (
                f"初步建议：{'、'.join(sizes)} 码（置信度{confidence}）。\n"
                "依据该商品公开尺码标签进行初筛；如处于两个尺码交界，请按修身或宽松偏好选择。\n"
                "线上建议仅供参考，实际合身度以试穿为准。"
            )
        return tool_result(ok=True, message=message, data=data)
