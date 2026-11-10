#!/usr/bin/env python3
"""Import a private fashion demo from an existing snapshot, without crawling.

The generated catalogue, photos and source mapping must remain Git-ignored.
Only the existing snapshot's image URLs are downloaded. Source price/SKU/size
facts are preserved; missing specifications are never fabricated.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageDraw, ImageOps


PROJECT = Path(__file__).resolve().parents[1]
CATEGORIES = ("外套", "T恤/上衣", "裤装", "卫衣/针织", "衬衫", "裙装/吊带")
QUOTAS_72 = {"外套": 18, "T恤/上衣": 16, "裤装": 16, "卫衣/针织": 10, "衬衫": 7, "裙装/吊带": 5}
MAX_PHOTO_BYTES = 16 * 1024 * 1024
MIN_PHOTO_SIDE = 320
USER_AGENT = "XiaoruLocalFashionImport/1.0"
NOTICE = "第三方服装参考照片仅供本机演示；未验证公开再分发权限，不代表品牌合作或真实在售。"
COLLABORATION = re.compile(r"联名|(?:明星|代言)|(?<!男女)同款", re.IGNORECASE)
MARKETING = re.compile(r"潮牌|新款|高级感|设计感|超火|网红|品牌自制|品牌|20\d{2}(?:年)?", re.IGNORECASE)


def _load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return payload


def neutral_name(value: str, redact_terms: list[str]) -> str:
    value = str(value or "")
    for term in sorted({term for term in redact_terms if term}, key=len, reverse=True):
        value = re.sub(re.escape(term), "", value, flags=re.IGNORECASE)
    value = re.sub(r"[【「『\[][^】」』\]]*[】」』\]]", "", value)
    value = re.sub(r"[\u4e00-\u9fffA-Za-z]{2,8}联名", "", value)
    value = re.sub(r"^(?!男女)[\u4e00-\u9fffA-Za-z]{2,4}同款", "", value)
    value = re.sub(r"[\u4e00-\u9fffA-Za-z]{2,8}系列", "", value)
    value = MARKETING.sub("", value)
    value = re.sub(r"联名|同款|系列", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ·，,:：-")
    return value[:64] or "服装参考单品"


def classify(name: str) -> str | None:
    """Use garment nouns before loose words such as 上衣 or 包, not source order."""
    # Accessories / household items must not become clothing because their
    # marketing title mentions the outfit that can accompany them.
    if re.search(r"冰袖|发夹|头巾|胸针|袜|(?<!连)帽子|冷帽|鸭舌帽|棒球帽|腰带|围巾|围脖|手套|耳饰|抱枕|冰箱贴|水杯|衣架|T恤配饰", name):
        return None
    if re.search(r"连体|连衣裙|吊带裙|半身裙|短裙|长裙|裙裤|百褶裙|[aA]字裙|蛋糕裙|伞裙|裙子|吊带|抹胸", name):
        return "裙装/吊带"
    if re.search(r"裤|卫裤|牛仔裤", name):
        return "裤装"
    if re.search(r"衬衫|衬衣|格子衫", name):
        return "衬衫"
    if re.search(r"羽绒|皮衣|皮夹克|夹克|棉服|风衣|大衣|棒球服|防晒服|防晒衣|麂皮.*外套", name):
        return "外套"
    if re.search(r"卫衣|毛衣|针织|开衫", name):
        return "卫衣/针织"
    if re.search(r"T恤|短袖|长袖|半袖|POLO|背心|罩衫|上衣", name, flags=re.IGNORECASE):
        return "T恤/上衣"
    if "外套" in name:
        return "外套"
    return None


def candidates_from_snapshot(dataset: dict, *, redact_terms: list[str], exclude_terms: list[str], skip_aliases: set[str]) -> list[dict]:
    candidates = []
    seen_urls = set()
    for position, raw in enumerate(dataset.get("products") or []):
        if not isinstance(raw, dict):
            continue
        original_name = str(raw.get("name") or "")
        alias = str(raw.get("source_product_alias") or raw.get("product_id") or "")
        url = str(raw.get("image_url") or "").strip()
        if not alias or alias in skip_aliases or url in seen_urls:
            continue
        if any(term.casefold() in original_name.casefold() for term in exclude_terms):
            continue
        category = classify(original_name)
        if not category or urlsplit(url).scheme != "https":
            continue
        price = raw.get("price")
        if price is None or not isinstance(price, (float, int)) or price <= 0:
            continue
        seen_urls.add(url)
        candidates.append({
            "raw": copy.deepcopy(raw), "alias": alias, "name": neutral_name(original_name, redact_terms),
            "category": category, "position": position, "image_url": url,
            "has_sku": bool(raw.get("variants")),
        })
    # Retain structured SKU facts first, then prefer modest title design claims.
    candidates.sort(key=lambda c: (not c["has_sku"], bool(re.search(r"大字母|满印|老花|logo|LOGO", c["name"])), c["position"]))
    return candidates


def candidate_batches(candidates: list[dict], count: int) -> list[list[dict]]:
    """Prepare a balanced first batch plus replacements for failed downloads."""
    quotas = {key: max(1, round(value * count / 72)) for key, value in QUOTAS_72.items()}
    selected = [c for c in candidates if c["has_sku"]][:count]
    used = {c["alias"] for c in selected}
    counts = Counter(c["category"] for c in selected)
    for category in CATEGORIES:
        for item in candidates:
            if len(selected) >= count:
                break
            if item["alias"] not in used and item["category"] == category and counts[category] < quotas[category]:
                selected.append(item)
                used.add(item["alias"])
                counts[category] += 1
    for item in candidates:
        if len(selected) >= count:
            break
        if item["alias"] not in used:
            selected.append(item)
            used.add(item["alias"])
    rest = [c for c in candidates if c["alias"] not in used]
    return [selected, rest]


def _image_bytes(url: str, timeout: float) -> bytes:
    last_error = None
    urls = [url]
    # The snapshot CDN uses these two public origins for the same file path.
    # Alternate its origin on TLS failure, without changing the garment image.
    location = urlsplit(url)
    if location.hostname in {"img01.yzcdn.cn", "img.yzcdn.cn"}:
        alternate = "img.yzcdn.cn" if location.hostname == "img01.yzcdn.cn" else "img01.yzcdn.cn"
        urls.append(url.replace(location.netloc, alternate, 1))
    for download_url in urls:
        try:
            with requests.get(download_url, headers={"User-Agent": USER_AGENT}, timeout=(min(timeout, 4), timeout), stream=True) as response:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(128 * 1024):
                    size += len(chunk)
                    if size > MAX_PHOTO_BYTES:
                        raise ValueError("Photo exceeds 16 MB")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    # Use Windows' official TLS implementation when OpenSSL fails against a CDN.
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if os.name == "nt" and curl.is_file():
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [str(curl), "--fail", "--location", "--silent", "--show-error", "--proto", "=https",
                 "--proto-redir", "=https", "--max-time", str(timeout), "--max-filesize", str(MAX_PHOTO_BYTES), url],
                stdout=output, stderr=subprocess.PIPE, timeout=timeout + 3,
                creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            )
            if result.returncode == 0 and 0 < output.tell() <= MAX_PHOTO_BYTES:
                output.seek(0)
                return output.read(MAX_PHOTO_BYTES)
    raise ValueError("Could not download/validate image") from last_error


def download_photo(candidate: dict, assets_dir: Path, timeout: float) -> dict:
    digest = hashlib.sha256(candidate["image_url"].encode()).hexdigest()[:20]
    filename = "garment-" + digest + ".jpg"
    target = assets_dir / filename
    if target.is_file():
        with Image.open(target) as image:
            if image.format == "JPEG" and min(image.size) >= MIN_PHOTO_SIDE:
                return {"filename": filename, "width": image.width, "height": image.height, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    raw = _image_bytes(candidate["image_url"], timeout)
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 40_000_000 or min(source.size) < MIN_PHOTO_SIDE:
            raise ValueError("Photo is too small or too large")
        image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    assets_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    image.save(temporary, format="JPEG", quality=90, optimize=True)
    temporary.replace(target)
    return {"filename": filename, "width": image.width, "height": image.height, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def normalize_item(candidate: dict, photo: dict, redact_terms: list[str]) -> tuple[dict, dict]:
    raw = candidate["raw"]
    product_id = "YZREF-" + re.sub(r"[^A-Za-z0-9]", "", candidate["alias"]).upper()
    image_url = "/static/images/catalog-local/" + photo["filename"]
    style_words = [word for word in ("简约", "复古", "通勤", "街头", "运动", "休闲", "宽松", "修身", "直筒", "阔腿", "印花", "条纹", "拼接", "连帽", "翻领", "圆领", "高腰") if word in candidate["name"]]
    item = {
        "product_id": product_id, "source_product_alias": candidate["alias"],
        "name": candidate["name"], "category": candidate["category"],
        "category_source": "local_garment_noun_classification", "price": raw["price"],
        "currency": raw.get("currency") or "CNY", "image_url": image_url,
        "picture_urls": [image_url], "source_type": "private_local_fashion_reference",
        "snapshot_at": raw.get("snapshot_at"), "ownership_verified": False,
        "detail_enriched": bool(raw.get("variants")), "tryon_supported": True,
        "tryon_garment_photo_type": "model", "service_labels_public": [],
        "selling_point": " · ".join(style_words[:3]) or candidate["category"],
        "subtitle": "本机服装参考 · 沙盒体验",
        "tryon_category": "bottoms" if candidate["category"] == "裤装" or re.search(r"半身裙|短裙|长裙|裙裤|百褶裙|[aA]字裙|蛋糕裙|伞裙|裙子", candidate["name"]) else "one-pieces" if re.search(r"连衣裙|吊带裙|连体", candidate["name"]) else "tops",
        "image_attribution": {"source_type": "private_local_reference", "rights_verified": False, "notice": NOTICE},
    }
    # Price and stock stay as recorded, with no invented stock/size entries.
    for key in ("price_max", "stock_total_public", "stock_source", "variants", "support_unconditional_return_public", "support_quick_refund_public", "support_barter_public"):
        if key in raw:
            item[key] = copy.deepcopy(raw[key])
    for variant in item.get("variants") or []:
        variant["attributes"] = {key: neutral_name(str(value), redact_terms) for key, value in (variant.get("attributes") or {}).items()}
    mapping = {"product_id": product_id, "original_name": raw.get("name"), "original_source_url": raw.get("source_url") or raw.get("public_url"),
               "original_image_url": candidate["image_url"], "local_image_url": image_url, "photo": photo,
               "license": "not_verified_for_public_redistribution", "processing": "EXIF orientation; fit within 1600px; JPEG conversion. No crop, logo removal or generative edits."}
    return item, mapping


def write_contact_sheet(products: list[dict], assets_dir: Path, output: Path) -> None:
    from PIL import ImageFont
    candidates = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts" / "msyh.ttc", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    font = next((ImageFont.truetype(str(path), 15) for path in candidates if path.is_file()), ImageFont.load_default())
    cell_width, cell_height, columns = 240, 290, 6
    sheet = Image.new("RGB", (columns * cell_width, ((len(products) + columns - 1) // columns) * cell_height), "#eceef0")
    for index, product in enumerate(products):
        with Image.open(assets_dir / product["image_url"].rsplit("/", 1)[1]) as source:
            thumb = ImageOps.contain(source.convert("RGB"), (cell_width - 10, 230))
        tile = Image.new("RGB", (cell_width, cell_height), "white")
        tile.paste(thumb, ((cell_width - thumb.width) // 2, 0))
        draw = ImageDraw.Draw(tile)
        draw.text((5, 232), f"{index + 1:02d} {product['category']} ￥{product['price']:g}", fill="#20252b", font=font)
        draw.text((5, 253), product["name"][:14], fill="#20252b", font=font)
        draw.text((5, 272), product["product_id"][-12:], fill="#596575", font=font)
        sheet.paste(tile, ((index % columns) * cell_width, (index // columns) * cell_height))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, format="JPEG", quality=90)


def import_catalog(*, source_data: Path, output_data: Path, assets_dir: Path, manifest_dir: Path, count: int,
                   redact_terms: list[str], exclude_terms: list[str], skip_aliases: set[str], workers: int = 4, timeout: float = 10) -> dict:
    source_data = source_data.resolve()
    source_root = source_data.parent.parent if source_data.name == "data" and source_data.parent.name == "agent" else source_data
    for output in (output_data, assets_dir, manifest_dir):
        target = output.resolve()
        if target == source_root or target.is_relative_to(source_root) or source_data.is_relative_to(target):
            raise ValueError("Import outputs must stay outside the original source project")
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_data.glob("*.json")}
    dataset = _load(source_data / "products_youzan_public.json")
    profile = _load(source_data / "store_profile.json")
    source_store_name = str(profile.get("store_name") or dataset.get("store_name") or "")
    terms = list(redact_terms)
    if source_store_name:
        terms += [source_store_name] + re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fff]+", source_store_name)
    candidates = candidates_from_snapshot(dataset, redact_terms=terms, exclude_terms=exclude_terms, skip_aliases=skip_aliases)
    import_all = count == 0
    first, replacements = (candidates, []) if import_all else candidate_batches(candidates, count)
    products, mappings, failures = [], [], []
    successful_aliases = set()

    def collect(batch):
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
            pending = {executor.submit(download_photo, candidate, assets_dir, timeout): candidate for candidate in batch}
            for future in as_completed(pending):
                candidate = pending[future]
                try:
                    photo = future.result()
                    item, mapping = normalize_item(candidate, photo, terms)
                    products.append(item)
                    mappings.append(mapping)
                    successful_aliases.add(candidate["alias"])
                    if len(products) % 25 == 0:
                        print(f"Validated {len(products)}/{len(candidates) if import_all else count} garment photos; {len(failures)} skipped", flush=True)
                except Exception as exc:
                    failures.append({"alias": candidate["alias"], "error_type": type(exc).__name__,
                                     "reason": str(exc)[:120] if isinstance(exc, ValueError) else "Image validation/download error"})

    collect(first)
    while len(products) < count and replacements:
        counts = Counter(p["category"] for p in products)
        quotas = {key: max(1, round(value * count / 72)) for key, value in QUOTAS_72.items()}
        replacements.sort(key=lambda c: (counts[c["category"]] >= quotas[c["category"]], not c["has_sku"], c["position"]))
        needed = count - len(products)
        batch, replacements = replacements[:needed], replacements[needed:]
        collect(batch)
    if not products or (not import_all and len(products) != count):
        raise RuntimeError(f"Only {len(products)} photos validated; requested {count}. Output catalog not changed.")
    if source_hashes != {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_data.glob("*.json")}:
        raise RuntimeError("Source snapshot changed during import; output catalog not changed")
    products.sort(key=lambda p: next(c["position"] for c in candidates if c["alias"] == p["source_product_alias"]))
    local_profile = {"schema_version": 1, "store_name": "小如的衣橱", "subtitle": "当季穿搭 · 本机服装参考", "dataset_label": "本机第三方服装参考目录（非真实在售）",
                     "source_type": "private_local_fashion_reference", "snapshot_at": dataset.get("snapshot_at"), "ownership_verified": False,
                     "catalog_items_collected": len(products), "ownership_notice": NOTICE,
                     "transaction_notice": "价格、库存与尺码来自历史参考记录；下单、余额、物流和退款均为沙盒模拟。"}
    # Only explicitly registered flat-lay images may override model covers.
    references = []
    source_refs = source_data / "tryon_references.json"
    if source_refs.is_file():
        for reference in _load(source_refs).get("references") or []:
            item = next((p for p in products if p["product_id"] == reference.get("product_id")), None)
            if not item:
                continue
            url = str(reference.get("tryon_image_url") or "")
            if urlsplit(url).scheme != "https":
                continue
            try:
                photo = download_photo({"image_url": url}, assets_dir, timeout)
            except Exception:
                continue
            record = {key: reference[key] for key in ("product_id", "tryon_category", "tryon_garment_photo_type", "color_label") if key in reference}
            record["tryon_image_url"] = "/static/images/catalog-local/" + photo["filename"]
            references.append(record)
            mappings.append({"product_id": item["product_id"], "reference_image_url": url, "local_image_url": record["tryon_image_url"], "photo": photo, "license": "not_verified_for_public_redistribution"})
    output_data.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    def save(path: Path, value):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            temporary = Path(stream.name)
        temporary.replace(path)
    save(output_data / "products_youzan_public.json", {"schema_version": 1, "dataset_label": local_profile["dataset_label"], "source_type": local_profile["source_type"], "snapshot_at": dataset.get("snapshot_at"), "products": products})
    save(output_data / "store_profile.json", local_profile)
    save(output_data / "tryon_references.json", {"references": references})
    save(manifest_dir / "source_mapping.json", {"source_store_profile": profile, "source_sha256": source_hashes,
                                               "source_unchanged_during_import": True,
                                               "rights_notice": NOTICE, "items": mappings, "download_failures": failures})
    write_contact_sheet(products[:72], assets_dir, manifest_dir / "contactsheet.jpg")
    for page, start in enumerate(range(72, len(products), 72), 2):
        write_contact_sheet(products[start:start + 72], assets_dir, manifest_dir / f"contactsheet-{page:02d}.jpg")
    return {"eligible_candidates": len(candidates), "products": len(products), "categories": dict(Counter(p["category"] for p in products)), "with_sku": sum(bool(p.get("variants")) for p in products),
            "tryon_supported": sum(p["tryon_supported"] for p in products), "verified_references": len(references), "download_failures_replaced": len(failures),
            "catalog": str(output_data / "products_youzan_public.json"), "contactsheet": str(manifest_dir / "contactsheet.jpg")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True, help="Existing snapshot directory; never modified")
    parser.add_argument("--output-data", type=Path, default=PROJECT / "agent" / "data")
    parser.add_argument("--assets-dir", type=Path, default=PROJECT / "gui" / "static" / "images" / "catalog-local")
    parser.add_argument("--manifest-dir", type=Path, default=PROJECT / "cache_data" / "local-catalog-import")
    parser.add_argument("--limit", "--count", dest="count", type=int, default=0, help="0 imports all wearable clothing; positive values select a balanced subset")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--redact-term", action="append", default=[], help="Additional source brand/series token to remove from display names")
    parser.add_argument("--exclude-term", action="append", default=[], help="Exclude titles containing this collaboration/logo token")
    parser.add_argument("--skip-alias", action="append", default=[], help="Exclude a visually rejected candidate")
    args = parser.parse_args()
    if not 0 <= args.count <= 5000:
        parser.error("--limit must be between 0 and 5000")
    result = import_catalog(source_data=args.source_data, output_data=args.output_data, assets_dir=args.assets_dir,
                            manifest_dir=args.manifest_dir, count=args.count, redact_terms=args.redact_term,
                            exclude_terms=args.exclude_term, skip_aliases=set(args.skip_alias), workers=args.workers, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
