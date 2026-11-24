from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def load_json(name: str) -> Any:
    path = DATA_DIR / name
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def tool_result(*, ok: bool, message: str, data: Any = None, error: str | None = None) -> dict:
    return {
        "ok": ok,
        "message": message,
        "data": data,
        "error": error,
    }


def requested_gender(query: str) -> str | None:
    """Use the shopping recipient before the speaker's explicit self-description."""
    recipient = re.search(
        r"(?:给|替|为|帮)\s*(?:我(?:的)?|自己(?:的)?)?"
        r"(?P<person>女朋友|老婆|妻子|妈妈|母亲|女儿|男朋友|老公|丈夫|爸爸|父亲|儿子)",
        query,
    )
    if recipient:
        return "女" if recipient["person"] in {"女朋友", "老婆", "妻子", "妈妈", "母亲", "女儿"} else "男"
    preference = re.search(r"(?:想要|想看|只要|只选|换成|改成|要买|买件|买个)\s*(男款|男装|女款|女装)", query)
    if preference:
        return preference[1][0]
    speaker = re.search(r"(?:我(?:自己)?是|本人(?:是)?)\s*(?:一[个名位])?([男女])(?:的|生|士|性|孩子)?", query)
    if speaker:
        return speaker[1]
    affirmative = re.sub(r"(?:不要|不选|不想要|不喜欢|排除|别选)\s*(?:男款|男装|女款|女装)", "", query)
    male = "男" in affirmative
    female = "女" in affirmative
    if male == female:
        return None
    return "男" if male else "女"


def _gender_label(value: object) -> str | None:
    """Normalize catalog gender values and explicit product title labels."""
    text = str(value or "").strip().casefold()
    if text in {"unisex", "neutral", "中性", "不限", "通用"} or re.search(
            r"中性|情侣|男女|unisex", text):
        return "中性"
    if text in {"male", "man", "men", "mens", "男"}:
        return "男"
    if text in {"female", "woman", "women", "womens", "女"}:
        return "女"
    # A style described as "男友风" does not establish who the garment is for.
    text = re.sub(r"男友风|女友风", "", text)
    male = bool(re.search(r"男(?:款|装|士|生|性|版|用|童)", text))
    female = bool(re.search(r"女(?:款|装|士|生|性|版|用|童)", text))
    if male != female:
        return "男" if male else "女"
    if not male and ("男" in text) != ("女" in text):
        return "男" if "男" in text else "女"
    return None


def product_gender(product: str | dict) -> str | None:
    """Prefer explicit catalog/SKU attributes; leave unknown labels unknown."""
    if isinstance(product, str):
        return _gender_label(product)
    values = [product.get(key) for key in ("gender", "target_gender", "适用性别", "性别")]
    attributes = [product.get("attributes") or {}]
    if isinstance(product.get("category_properties_public"), dict):
        attributes.append(product["category_properties_public"])
    attributes.extend(variant.get("attributes") or {} for variant in product.get("variants", []))
    for mapping in attributes:
        values.extend(value for key, value in mapping.items()
                      if str(key).casefold() in {"gender", "target_gender", "适用性别", "性别", "适用人群"})
    labels = {_gender_label(value) for value in values} - {None}
    if len(labels) == 1:
        return labels.pop()
    if labels:
        return None
    return _gender_label(product.get("name"))


def matches_requested_gender(name: str | dict, gender: str | None) -> bool:
    if not gender:
        return True
    label = product_gender(name)
    return label in {_gender_label(gender), "中性"}
