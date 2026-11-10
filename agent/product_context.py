"""Normalize the optional product context sent by a storefront control."""

from __future__ import annotations

import re


_PRODUCT_CONTEXT_PATTERN = re.compile(r"(?:YZREF-[A-Z0-9]{1,48}|EC[0-9]{4,12})\Z")


def normalize_product_context_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("商品编号格式无效")
    normalized = value.strip().upper()
    if not _PRODUCT_CONTEXT_PATTERN.fullmatch(normalized):
        raise ValueError("商品编号格式无效")
    return normalized
