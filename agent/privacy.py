"""客服请求中的最小化个人数据处理。"""

from __future__ import annotations

import re
from typing import Any


_LABELED_BODY_VALUE = re.compile(
    r"(?P<label>身高|体重|胸围|腰围|臀围|肩宽|裤长|上胸围|下胸围)"
    r"\s*(?:是|为|[:：])?\s*"
    r"(?P<value>\d{2,3}(?:\.\d+)?)\s*"
    r"(?P<unit>cm|厘米|kg|公斤|千克|斤)?",
    re.IGNORECASE,
)
_UNIT_BODY_VALUE = re.compile(
    r"(?<![A-Za-z0-9])\d{2,3}(?:\.\d+)?\s*(?:cm|厘米|kg|公斤|千克|斤)(?![A-Za-z])",
    re.IGNORECASE,
)
_SLASH_BODY_VALUE = re.compile(r"(?<!\d)\d{3}(?:\.\d+)?\s*[/／]\s*\d{2,3}(?:\.\d+)?(?!\d)")
_SENSITIVE_KEYS = {
    "height",
    "height_cm",
    "weight",
    "weight_kg",
    "chest",
    "chest_cm",
    "waist",
    "waist_cm",
    "hip",
    "hip_cm",
    "shoulder",
    "shoulder_cm",
    "demo_session_id",
    "body_type",
    "attachment_id",
}


def contains_body_measurements(text: object) -> bool:
    value = str(text or "")
    return bool(
        _LABELED_BODY_VALUE.search(value)
        or _UNIT_BODY_VALUE.search(value)
        or _SLASH_BODY_VALUE.search(value)
    )


def redact_body_measurements(text: object) -> str:
    value = str(text or "")
    value = _LABELED_BODY_VALUE.sub(lambda match: f"{match.group('label')}[已脱敏]", value)
    value = _UNIT_BODY_VALUE.sub("[身体数据已脱敏]", value)
    value = re.sub(r'肩部较宽|腰腹较丰满|胯部较宽|胯宽|梨形|偏瘦|匀称', '[体型偏好已脱敏]', value)
    return _SLASH_BODY_VALUE.sub("[身体数据已脱敏]", value)


def redact_sensitive_arguments(value: Any, *, key: str | None = None) -> Any:
    """递归清理审计/日志参数，不修改工具实际收到的请求。"""
    if key and key.lower() in _SENSITIVE_KEYS:
        return "[已脱敏]"
    if isinstance(value, str):
        return redact_body_measurements(value)
    if isinstance(value, dict):
        return {
            item_key: redact_sensitive_arguments(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_arguments(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_arguments(item) for item in value)
    return value
