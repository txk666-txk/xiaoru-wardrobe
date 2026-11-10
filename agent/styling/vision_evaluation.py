"""Offline dataset validation and clothing-observation evaluation.

This module never uploads photos, builds a dataset from customer sessions, or
trains a model. Reports contain labels and sample IDs, never image paths/data.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import time
from collections import Counter
from pathlib import Path, PureWindowsPath

from PIL import Image, ImageOps, UnidentifiedImageError

from .vision_contract import ATTRIBUTE_VALUES, CATEGORIES, PROMPT_VERSION, build_vision_prompt

VALUES = ATTRIBUTE_VALUES
FIELDS = ("color", "pattern", "fit")
SPLITS = ("train", "validation", "test")
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MISSING = "__missing__"


class DatasetError(ValueError):
    """A manifest or offline prediction does not meet the experiment contract."""


def _jsonl(path):
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DatasetError("Cannot read the JSONL input") from exc
    records = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise DatasetError(f"Line {number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise DatasetError(f"Line {number}: expected a JSON object")
        records.append(row)
    if not records:
        raise DatasetError("JSONL input contains no samples")
    return records


def _identifier(value, name):
    if (not isinstance(value, str) or not value.strip() or len(value) > 160
            or any(ord(character) < 32 for character in value)):
        raise DatasetError(f"{name}: expected a non-empty identifier of at most 160 characters")
    return value


def validate_garments(garments, *, labels=True):
    """Validate declared labels without forgiving invalid or missing values.

    Predictions may omit an attribute: it is scored as unknown, not discarded.
    Unknown *category* is not meaningful; images with no garments use [].
    """
    if not isinstance(garments, list) or len(garments) > 4:
        raise DatasetError("garments: expected a list of at most four observations")
    result = []
    for item in garments:
        if not isinstance(item, dict) or item.get("category") not in CATEGORIES:
            raise DatasetError("garments: unsupported category")
        row = {"category": item["category"]}
        for field in FIELDS:
            value = item.get(field, None if labels else "未知")
            if value not in VALUES[field]:
                raise DatasetError(f"garments: invalid or missing {field}")
            row[field] = value
        if "style" in item:
            if item["style"] not in VALUES["style"]:
                raise DatasetError("garments: invalid style")
            row["style"] = item["style"]
        result.append(row)
    return result


def _image_path(root, relative):
    if not isinstance(relative, str) or not relative.strip():
        raise DatasetError("image: expected a relative local path")
    windows = PureWindowsPath(relative)
    path = Path(relative)
    if (path.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in windows.parts or ".." in path.parts or ":" in relative
            or "\x00" in relative):
        raise DatasetError("image: absolute paths, parent traversal and URLs are not allowed")
    try:
        resolved = (root / path).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise DatasetError("image: file must exist inside the manifest directory") from exc
    if not resolved.is_file():
        raise DatasetError("image: expected a file")
    return resolved


def _read_image(path):
    try:
        if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise DatasetError("image: size must be between 1 byte and 64 MiB")
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise DatasetError("image: use static JPEG, PNG or WebP")
            if min(image.size) < 128 or image.width * image.height > 24_000_000:
                raise DatasetError("image: dimensions must be at least 128 px and at most 24 million pixels")
            return ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise DatasetError("image: not a readable static image") from exc


def validate_manifest(path):
    """Return records with private ``_image_path`` and ``_pixel_hash`` fields.

    Every image must be inside the manifest's directory. All split labels are
    checked before selecting a split, so evaluation cannot hide leakage from
    the training set. Hashes catch identical decoded pixels, not near duplicates.
    Use group IDs for near duplicates, the same garment, and person identity.
    """
    path = Path(path).resolve()
    root = path.parent
    samples = _jsonl(path)
    sample_ids, group_splits, image_splits = set(), {}, {}
    validated = []
    for row in samples:
        sample_id = _identifier(row.get("sample_id"), "sample_id")
        if sample_id in sample_ids:
            raise DatasetError("sample_id: duplicate identifier")
        sample_ids.add(sample_id)
        split = row.get("split")
        if split not in SPLITS:
            raise DatasetError("split: choose train, validation or test")
        if not isinstance(row.get("mode"), str) or row["mode"] not in {"person", "garment"}:
            raise DatasetError("mode: choose person or garment")
        data_kind = row.get("data_kind", "real")
        if not isinstance(data_kind, str) or data_kind not in {"real", "smoke"}:
            raise DatasetError("data_kind: choose real or smoke")
        groups = []
        if "group_id" in row:
            groups.append(_identifier(row["group_id"], "group_id"))
        if "group_ids" in row:
            if not isinstance(row["group_ids"], list) or not row["group_ids"]:
                raise DatasetError("group_ids: expected a non-empty list")
            groups.extend(_identifier(value, "group_ids") for value in row["group_ids"])
        if not groups:
            raise DatasetError("group_id or group_ids is required to prevent split leakage")
        for group in groups:
            if group_splits.setdefault(group, split) != split:
                raise DatasetError("group leakage: a group occurs in multiple splits")
        garments = validate_garments(row.get("garments"))
        image_path = _image_path(root, row.get("image"))
        image = _read_image(image_path)
        digest = hashlib.sha256()
        digest.update(f"RGB:{image.width}x{image.height}:".encode("ascii"))
        digest.update(image.tobytes())
        pixel_hash = digest.hexdigest()
        if image_splits.setdefault(pixel_hash, split) != split:
            raise DatasetError("image leakage: identical image pixels occur in multiple splits")
        validated.append({**row, "garments": garments, "data_kind": row.get("data_kind", "real"),
                          "_image_path": image_path, "_pixel_hash": pixel_hash})
    return validated


def manifest_summary(samples):
    return {"samples": len(samples), "splits": dict(Counter(row["split"] for row in samples)),
            "data_kinds": dict(Counter(row["data_kind"] for row in samples)),
            "category_counts": dict(Counter(item["category"] for row in samples for item in row["garments"])),
            "image_hash_check": "exact decoded RGB pixels; grouped near-duplicates require correct group IDs"}


def normalized_image_bytes(path):
    """Match the live styling upload's EXIF/RGB/768px/JPEG90 preprocessing."""
    image = _read_image(Path(path))
    image.thumbnail((768, 768), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    return output.getvalue()


def _pairs(expected, predicted):
    """Category-constrained optimal attribute matching, at most 4 garments.

    Observations have no bounding boxes; this is set matching, not localization.
    Attribute agreement breaks ties for repeated categories deterministically.
    """
    matched, used_expected, used_predicted = [], set(), set()
    for category in CATEGORIES:
        truth = [i for i, item in enumerate(expected) if item["category"] == category]
        guesses = [i for i, item in enumerate(predicted) if item["category"] == category]
        size = min(len(truth), len(guesses))
        if not size:
            continue
        best, best_score = None, -1
        for truth_order in itertools.combinations(truth, size):
            for guess_order in itertools.permutations(guesses, size):
                pairs = tuple(zip(truth_order, guess_order))
                score = sum(expected[t].get(field) == predicted[p].get(field)
                            for t, p in pairs for field in FIELDS)
                if score > best_score:
                    best, best_score = pairs, score
        matched.extend(best)
        used_expected.update(t for t, _ in best)
        used_predicted.update(p for _, p in best)
    return matched, set(range(len(expected))) - used_expected, set(range(len(predicted))) - used_predicted


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _prf(tp, fp, fn):
    return {"tp": tp, "fp": fp, "fn": fn, "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn), "f1": _ratio(2 * tp, 2 * tp + fp + fn)}


def _classification(pairs, labels):
    result = {}
    for label in labels:
        tp = sum(t == label and p == label for t, p in pairs)
        fp = sum(t != label and p == label for t, p in pairs)
        fn = sum(t == label and p != label for t, p in pairs)
        if tp + fp + fn:
            result[label] = _prf(tp, fp, fn)
    values = [entry["f1"] for entry in result.values() if entry["f1"] is not None]
    return {"macro_f1": sum(values) / len(values) if values else None, "per_class": result}


def percentile(values, percentile_value):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percentile_value
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def score_predictions(samples, predictions):
    """Score selected samples; missing/invalid predictions count as failures.

    ``predictions`` maps sample_id to {garments: [...]} or {error: code}.
    All expected garments from failed calls count as missed, not excluded.
    """
    categories, attributes = [], {field: [] for field in FIELDS}
    details, errors, latencies = [], Counter(), []
    exact = 0
    matched_attributes = {field: Counter() for field in FIELDS}
    extra_garments = missed_garments = 0
    for sample in samples:
        sample_id = sample["sample_id"]
        expected = sample["garments"]
        entry = predictions.get(sample_id, {"error": "missing_prediction"})
        error = entry.get("error")
        predicted = []
        if not error:
            try:
                predicted = validate_garments(entry.get("garments"), labels=False)
            except DatasetError:
                error = "invalid_prediction"
        if error:
            error = str(error)[:80]
            errors[error] += 1
        elapsed = entry.get("latency_seconds")
        if (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                and math.isfinite(elapsed) and elapsed >= 0):
            latencies.append(elapsed)
        pairs, missed, extras = _pairs(expected, predicted)
        missed_garments += len(missed)
        extra_garments += len(extras)
        for truth, guess in pairs:
            categories.append((expected[truth]["category"], predicted[guess]["category"]))
            for field in FIELDS:
                target, output = expected[truth][field], predicted[guess][field]
                attributes[field].append((target, output))
                stats = matched_attributes[field]
                if target == "未知":
                    stats["unknown_targets"] += 1
                    stats["unsupported_claims"] += output != "未知"
                else:
                    stats["known_targets"] += 1
                    stats["unknown_outputs_on_known"] += output == "未知"
        for truth in missed:
            categories.append((expected[truth]["category"], MISSING))
            for field in FIELDS:
                value = expected[truth][field]
                attributes[field].append((value, MISSING))
                matched_attributes[field]["unknown_targets" if value == "未知" else "known_targets"] += 1
        for guess in extras:
            categories.append((MISSING, predicted[guess]["category"]))
            for field in FIELDS:
                attributes[field].append((MISSING, predicted[guess][field]))
        is_exact = (not error and not missed and not extras
                    and all(expected[t][field] == predicted[p][field] for t, p in pairs for field in FIELDS))
        exact += is_exact
        detail = {"sample_id": sample_id, "expected": expected, "predicted": predicted,
                  "exact_match": bool(is_exact), "error": error,
                  "missed_garments": len(missed), "extra_garments": len(extras)}
        if elapsed is not None:
            detail["latency_seconds"] = elapsed
        details.append(detail)
    category_metrics = _classification(categories, CATEGORIES)
    total_tp = sum(entry["tp"] for entry in category_metrics["per_class"].values())
    total_fp = sum(entry["fp"] for entry in category_metrics["per_class"].values())
    total_fn = sum(entry["fn"] for entry in category_metrics["per_class"].values())
    category_metrics.update(_prf(total_tp, total_fp, total_fn))
    attribute_metrics = {}
    for field in FIELDS:
        counts = matched_attributes[field]
        field_pairs = attributes[field]
        labelled = [(t, p) for t, p in field_pairs if t != MISSING]
        attribute_metrics[field] = {**_classification(field_pairs, VALUES[field]),
            "correct": sum(t == p for t, p in labelled), "labelled_targets": len(labelled),
            "accuracy": _ratio(sum(t == p for t, p in labelled), len(labelled)),
            "unknown_targets": counts["unknown_targets"], "known_targets": counts["known_targets"],
            "unsupported_claims": counts["unsupported_claims"],
            "unsupported_attribute_rate_on_unknown_targets": _ratio(counts["unsupported_claims"], counts["unknown_targets"]),
            "unknown_outputs_on_known_targets": counts["unknown_outputs_on_known"],
            "unknown_output_rate_on_known_targets": _ratio(counts["unknown_outputs_on_known"], counts["known_targets"])}
    return {"sample_count": len(samples), "failed_samples": sum(errors.values()),
            "failure_rate": _ratio(sum(errors.values()), len(samples)), "error_counts": dict(errors),
            "exact_match_accuracy": _ratio(exact, len(samples)), "category": category_metrics,
            "attributes": attribute_metrics, "missed_garments": missed_garments, "extra_garments": extra_garments,
            "latency_seconds": {"count": len(latencies), "p50": percentile(latencies, .5), "p95": percentile(latencies, .95)},
            "samples": details}


def load_predictions(path, sample_ids):
    records = _jsonl(path)
    result = {}
    for record in records:
        sample_id = _identifier(record.get("sample_id"), "sample_id")
        if sample_id not in sample_ids:
            raise DatasetError("predictions: unknown sample_id for selected split")
        if sample_id in result:
            raise DatasetError("predictions: duplicate sample_id")
        # Offline timings are untrusted: do not report them as measured latency.
        if record.get("error"):
            result[sample_id] = {"error": "recorded_model_error"}
        else:
            try:
                result[sample_id] = {"garments": validate_garments(record.get("garments"), labels=False)}
            except DatasetError:
                result[sample_id] = {"error": "invalid_prediction"}
    return result


def run_model(samples, vision):
    from .vision import StylingError

    predictions = {}
    for sample in samples:
        image_bytes = normalized_image_bytes(sample["_image_path"])
        started = time.perf_counter()
        try:
            observation = vision.observe(image_bytes, sample["mode"], strict=True)
            entry = {"garments": observation["garments"]}
        except StylingError as exc:
            # ``observe`` returns [] for a valid no-garment response; unlike the
            # customer-facing ``analyze``, it must not turn that into an error.
            entry = {"error": exc.code}
        except (ValueError, KeyError, TypeError):
            entry = {"error": "invalid_prediction"}
        entry["latency_seconds"] = time.perf_counter() - started
        predictions[sample["sample_id"]] = entry
    return predictions


METRIC_DEFINITIONS = {
    "matching": "Category-constrained set matching; maximize color/pattern/fit agreement for duplicate categories. No localization metric.",
    "failure": "Missing, invalid and failed predictions remain in denominators; their target garments count as misses.",
    "category": "Micro precision/recall/F1 of garment categories, including missing and extra garments; macro F1 over labels appearing in truth or predictions.",
    "attribute_macro_f1": "Per-field macro F1 over observed ground-truth or predicted classes, including 未知; missed and extra garments create FN/FP.",
    "attribute_accuracy": "Correct values / all labelled ground-truth garment slots. Missed garments are incorrect; extra garments are penalized by F1, not this accuracy.",
    "unsupported_attribute_rate_on_unknown_targets": "Known attribute asserted where matched ground truth is 未知 / all ground-truth 未知 slots. Not a general factual hallucination rate; inspect extra garments and failure rate separately.",
    "unknown_output_rate_on_known_targets": "Matched output 未知 where ground truth is known / all known ground-truth slots. Missing garments are reported separately.",
    "latency": "Measured real model calls only, including failed calls; linear-interpolated p50/p95. Includes HTTP/model time, excludes image preprocessing. Offline prediction timings are ignored.",
    "empty_images": "Valid empty garment predictions match an empty target; inference errors never count as exact matches even on empty targets.",
    "schema_validation": "Category and declared enum values are validated. Missing optional predicted attributes score as 未知; this report does not measure complete-output-schema compliance.",
}


def build_report(manifest, samples, predictions, *, metadata, split):
    has_smoke = any(sample["data_kind"] == "smoke" for sample in samples)
    fingerprint = [{"sample_id": sample["sample_id"], "mode": sample["mode"],
                    "garments": sample["garments"], "pixel_sha256": sample["_pixel_hash"],
                    "data_kind": sample["data_kind"]}
                   for sample in sorted(samples, key=lambda sample: sample["sample_id"])]
    dataset_hash = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
    prompt_hashes = {variant: {mode: hashlib.sha256(build_vision_prompt(mode, variant).encode("utf-8")).hexdigest()
                              for mode in ("person", "garment")}
                     for variant in ("baseline", "domain")}
    experiment = {**dict(metadata), "prompt_version": PROMPT_VERSION, "prompt_sha256": prompt_hashes}
    return {"schema_version": 1, "experiment": experiment,
            "manifest_sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
            "selected_dataset_sha256": dataset_hash,
            "selected_split": split, "dataset": manifest_summary(samples),
            "benchmark_eligible": not has_smoke,
            "notice": ("SMOKE DATA: pipeline verification only; these scores are not model capability evidence."
                       if has_smoke else "User-supplied annotations; interpret quality in light of data sampling and annotation protocol."),
            "preprocessing": "EXIF transpose, RGB, longest side <=768, JPEG quality 90; matches Fay styling uploads",
            "metric_definitions": METRIC_DEFINITIONS,
            "metrics": score_predictions(samples, predictions)}
