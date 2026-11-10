from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .vision import COLORS, STYLES, StylingError

SCENES = ("日常", "通勤", "面试", "约会", "旅行", "运动")


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
    return result


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
                   if key in {"style", "fit", "gender", "colors", "excluded_colors", "budget"}}
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO style_preferences VALUES (?,?)",
                       (owner, json.dumps(allowed, ensure_ascii=False)))
        return allowed

    def delete(self, owner):
        with self._connect() as db:
            db.execute("DELETE FROM style_preferences WHERE owner=?", (owner,))
