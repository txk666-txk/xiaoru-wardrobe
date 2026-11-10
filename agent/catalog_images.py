"""Resolve registered catalog photos; never arbitrary local file paths."""
from __future__ import annotations

from pathlib import Path
import re


CATALOG_IMAGE_PREFIX = "/static/images/catalog/"
CATALOG_IMAGE_DIR = Path(__file__).resolve().parents[1] / "gui" / "static" / "images" / "catalog"
LOCAL_CATALOG_IMAGE_PREFIX = "/static/images/catalog-local/"
LOCAL_CATALOG_IMAGE_DIR = CATALOG_IMAGE_DIR.parent / "catalog-local"
MAX_CATALOG_IMAGE_BYTES = 16 * 1024 * 1024
_IMAGE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:jpg|jpeg|png|webp)", re.IGNORECASE)


def catalog_image_path(value: str) -> Path | None:
    """Return a registered photo, or None for a non-catalog URL.

    Only a single plain filename is accepted. Resolve symlinks before reading
    so neither encoded traversal nor a linked file can leave the photo folder.
    """
    value = str(value or "")
    directories = (
        (CATALOG_IMAGE_PREFIX, CATALOG_IMAGE_DIR),
        (LOCAL_CATALOG_IMAGE_PREFIX, LOCAL_CATALOG_IMAGE_DIR),
    )
    location = next(((prefix, directory) for prefix, directory in directories
                     if value.startswith(prefix)), None)
    if location is None:
        return None
    prefix, directory = location
    filename = value[len(prefix):]
    if not _IMAGE_FILENAME.fullmatch(filename):
        raise ValueError("无效的本地服装图片名称")
    root = directory.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        raise ValueError("本地服装图片不存在或超出登记目录")
    size = candidate.stat().st_size
    if not 0 < size <= MAX_CATALOG_IMAGE_BYTES:
        raise ValueError("本地服装图片大小异常")
    return candidate


def has_catalog_image(value: str) -> bool:
    try:
        return catalog_image_path(value) is not None
    except (OSError, ValueError):
        return False
