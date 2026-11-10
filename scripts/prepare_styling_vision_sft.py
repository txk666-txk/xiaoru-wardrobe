"""Export explicitly labelled wardrobe images for external VLM SFT tooling.

This prepares LLaMA-Factory ShareGPT datasets; it does not train a model.
The held-out test split is never exported as training/evaluation input here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.styling.vision_contract import PROMPT_VERSION, build_vision_prompt
from agent.styling.vision_evaluation import DatasetError, validate_manifest


FORMAT_REFERENCE = "https://github.com/hiyouga/LLaMA-Factory/blob/main/data/README.md#multimodal-image-dataset"


def training_record(sample: dict, prompt_variant: str) -> dict:
    """Copy only whitelisted labels, never arbitrary manifest fields."""
    garments = []
    for item in sample["garments"]:
        garments.append({
            "category": item["category"],
            "color": item["color"],
            "style": item.get("style", "未知"),
            "pattern": item["pattern"],
            "fit": item["fit"],
        })
    return {
        "conversations": [
            {"from": "human", "value": "<image>" + build_vision_prompt(sample["mode"], variant=prompt_variant)},
            {"from": "gpt", "value": json.dumps({"garments": garments}, ensure_ascii=False, separators=(",", ":"))},
        ],
        "images": [str(sample["_image_path"])],
    }


def prepare_dataset(manifest: str | Path, output_dir: str | Path,
                    prompt_variant: str = "domain") -> dict:
    """Validate all splits, then write to a new directory without overwriting."""
    if prompt_variant not in {"domain", "baseline"}:
        raise DatasetError("prompt_variant 必须为 domain 或 baseline")
    manifest_path = Path(manifest).resolve()
    samples = validate_manifest(manifest_path)
    if any(sample.get("data_kind", "real") == "smoke" for sample in samples):
        raise DatasetError("smoke 样本仅用于测试工具链，不能导出为真实 SFT 数据")
    split_counts = {split: sum(sample["split"] == split for sample in samples)
                    for split in ("train", "validation", "test")}
    if split_counts["train"] == 0:
        raise DatasetError("至少需要一条 train 样本；不能使用 validation 或 test 替代训练集")
    records = {
        split: [training_record(sample, prompt_variant) for sample in samples if sample["split"] == split]
        for split in ("train", "validation")
    }
    info = {}
    for split, rows in records.items():
        if rows:
            info["fay_styling_vision_" + split] = {
                "file_name": split + ".json",
                "formatting": "sharegpt",
                "columns": {"messages": "conversations", "images": "images"},
            }
    warnings = []
    if not split_counts["validation"]:
        warnings.append("没有 validation 数据：先划出验证集，再选择训练轮数和超参数。")
    if not split_counts["test"]:
        warnings.append("没有独立 test 数据：当前数据不能用于报告最终泛化效果。")
    summary = {
        "schema_version": 1,
        "format": "llamafactory_sharegpt_images",
        "format_reference": FORMAT_REFERENCE,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "prompt_variant": prompt_variant,
        "prompt_version": PROMPT_VERSION,
        "live_image_contract": {"max_edge_px": 768, "jpeg_quality": 90},
        "training_image_preprocessing": "external_training_framework",
        "split_counts": split_counts,
        "exported_splits": [split for split, rows in records.items() if rows],
        "test_exported": False,
        "images_copied": False,
        "training_executed": False,
        "warnings": warnings,
    }
    output = Path(output_dir).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DatasetError("输出目录已存在，请指定新的 --output-dir；工具不会覆盖已有数据") from exc
    for split, rows in records.items():
        if rows:
            (output / (split + ".json")).write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "dataset_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验显式标注图片并导出 VLM SFT 数据，不启动训练。")
    parser.add_argument("--manifest", required=True, help="包含 train/validation/test 标签的 JSONL")
    parser.add_argument("--output-dir", required=True, help="尚不存在的输出目录")
    parser.add_argument("--prompt-variant", choices=("domain", "baseline"), default="domain")
    args = parser.parse_args(argv)
    try:
        summary = prepare_dataset(args.manifest, args.output_dir, args.prompt_variant)
    except (DatasetError, OSError) as exc:
        parser.exit(2, "SFT 数据准备失败：" + str(exc) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
