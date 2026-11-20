from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .vision import COLORS, STYLES, StylingError

SCENES = ("日常", "通勤", "面试", "约会", "旅行", "运动")
COLOR_GROUPS = ("tops", "bottoms", "outerwear", "one-pieces", "shoes", "accessory")


def clean_preferences(payload):
    if not isinstance(payload, dict):
        raise StylingError("invalid_preferences", "穿搭选项必须是对象")
    result = {}
    for key, allowed in {"scene": SCENES, "style": STYLES, "fit": ("宽松", "合身", "修身"),
                         "gender": ("不限", "男款", "女款", "中性"),
                         "season": ("春季", "夏季", "秋季", "冬季"),
                         "body_type": ("肩部较宽", "腰腹较丰满", "胯部较宽", "偏瘦", "匀称")}.items():
        if payload.get(key) not in (None, ""):
            if payload[key] not in allowed:
                raise StylingError("invalid_preferences", "请从列表选择有效的场景、风格和版型")
            result[key] = payload[key]
    for key in ("colors", "excluded_colors"):
        if key in payload:
            values = payload[key]
            if not isinstance(values, list) or len(values) > 6 or any(value not in COLORS for value in values):
                raise StylingError("invalid_preferences", "请选择有效颜色，最多六种")
            result[key] = list(dict.fromkeys(values))
    for color_field in ("excluded_colors_by_group", "required_colors_by_group"):
        if color_field not in payload:
            continue
        groups = payload[color_field]
        if not isinstance(groups, dict) or any(key not in COLOR_GROUPS for key in groups):
            raise StylingError("invalid_preferences", "请提供有效的单品颜色限制")
        result[color_field] = {}
        for group, values in groups.items():
            if not isinstance(values, list) or len(values) > 6 or any(value not in COLORS for value in values):
                raise StylingError("invalid_preferences", "单品排除颜色最多六种，且必须为有效颜色")
            result[color_field][group] = list(dict.fromkeys(values))
    if "bottoms_kind" in payload:
        if not isinstance(payload["bottoms_kind"], str) or payload["bottoms_kind"] not in {"pants", "skirt"}:
            raise StylingError("invalid_preferences", "请提供有效的裤装或半身裙类型")
        result["bottoms_kind"] = payload["bottoms_kind"]
    if "budget_unlimited" in payload:
        if not isinstance(payload["budget_unlimited"], bool):
            raise StylingError("invalid_preferences", "预算不限必须为布尔值")
        result["budget_unlimited"] = payload["budget_unlimited"]
    for key, bounds in {"budget": (30, 10000), "temperature": (-30, 50),
                        "height_cm": (120, 220), "weight_kg": (30, 200)}.items():
        if payload.get(key) not in (None, ""):
            try:
                value = float(payload[key])
                if isinstance(payload[key], bool) or not bounds[0] <= value <= bounds[1]:
                    raise ValueError()
            except (TypeError, ValueError):
                raise StylingError("invalid_preferences", f"{key} 超出有效范围")
            result[key] = value
    if result.get("budget_unlimited"):
        result.pop("budget", None)
    return result


def merge_preferences(current, updates):
    """An explicit new numeric budget replaces 'unlimited', and vice versa."""
    clean = clean_preferences(updates)
    result = {**(current or {}), **clean}
    for color_field in ("excluded_colors_by_group", "required_colors_by_group"):
        if clean.get(color_field):
            result[color_field] = {**(current or {}).get(color_field, {}), **clean[color_field]}
    if "budget" in clean and not clean.get("budget_unlimited"):
        result.pop("budget_unlimited", None)
    return clean_preferences(result)


class PreferenceStore:
    """Persist only opt-in enum preferences, never photos or body measurements."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS style_preferences (owner TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                yield db
        finally:
            db.close()

    def get(self, owner):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM style_preferences WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, owner, payload, consent):
        if consent is not True:
            raise StylingError("preference_consent_required", "保存偏好前请明确同意；本次推荐无需保存")
        allowed = {key: value for key, value in clean_preferences(payload).items()
                   if key in {"style", "fit", "gender", "colors", "excluded_colors", "budget",
                              "budget_unlimited", "excluded_colors_by_group", "required_colors_by_group", "bottoms_kind"}}
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO style_preferences VALUES (?,?)",
                       (owner, json.dumps(allowed, ensure_ascii=False)))
        return allowed

    def delete(self, owner):
        with self._connect() as db:
            db.execute("DELETE FROM style_preferences WHERE owner=?", (owner,))
