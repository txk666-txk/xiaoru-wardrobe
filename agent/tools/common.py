from __future__ import annotations

import json
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
    """Only use an explicit preference; measurements never imply a gender."""
    male = "男" in query
    female = "女" in query
    if male == female:
        return None
    return "男" if male else "女"


def matches_requested_gender(name: str, gender: str | None) -> bool:
    if not gender:
        return True
    if "情侣" in name or "中性" in name or "男女" in name:
        return True
    opposite_labels = ("女款", "女装", "女士", "女生") if gender == "男" else (
        "男款", "男装", "男士", "男生"
    )
    matching_labels = ("男款", "男装", "男士", "男生") if gender == "男" else (
        "女款", "女装", "女士", "女生"
    )
    if any(label in name for label in opposite_labels) and not any(
        label in name for label in matching_labels
    ):
        return False
    return gender in name or "情侣" in name or "中性" in name
