"""Locate optional local character assets without distributing them."""

import os
import re
from pathlib import Path


VENDOR_FILES = (
    "vendor/live2dcubismcore.min.js",
    "vendor/pixi.min.js",
    "vendor/live2dcubismframework.js",
    "vendor/live2dcubismpixi.js",
    "vendor/l2d.js",
)


def avatar_template_context(static_folder: str | Path) -> dict:
    static_root = Path(static_folder)
    local_avatar = "images/customer-avatar-local.png"
    customer_avatar = local_avatar if (static_root / local_avatar).is_file() else "images/xiaoru-avatar.svg"
    model = os.environ.get("FAY_LIVE2D_MODEL", "xiaoru_1").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", model):
        model = "xiaoru_1"
    root = static_root / "live2d"
    available = all((root / path).is_file() for path in VENDOR_FILES)
    available = available and (root / "model" / model / f"{model}.model3.json").is_file()
    return {
        "live2d_assets_available": available,
        "live2d_model": model,
        "customer_avatar_asset": customer_avatar,
    }
