from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from agent.styling.vision import StylingError
from agent.styling.vision_contract import PROMPT_VERSION
from agent.styling.vision_evaluation import (DatasetError, build_report, load_predictions,
    normalized_image_bytes, run_model, score_predictions, validate_manifest)
from scripts.evaluate_styling_vision import main


def garment(category="tops", color="白", pattern="纯色", fit="合身"):
    return {"category": category, "color": color, "pattern": pattern, "fit": fit}


def sample(identifier="s1", garments=None):
    return {"sample_id": identifier, "garments": [garment()] if garments is None else garments,
            "split": "test", "mode": "garment", "data_kind": "real"}


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.jsonl"
        Image.new("RGB", (160, 160), "white").save(self.root / "white.png")
        Image.new("RGB", (160, 160), "black").save(self.root / "black.png")

    def tearDown(self):
        self.temp.cleanup()

    def row(self, identifier="s1", image="white.png", split="test", group="white"):
        return {"sample_id": identifier, "image": image, "split": split, "group_id": group,
                "mode": "garment", "garments": [garment()]}

    def write(self, rows):
        self.manifest.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    def test_manifest_decodes_images_and_returns_private_paths(self):
        self.write([self.row()])
        rows = validate_manifest(self.manifest)
        self.assertEqual(rows[0]["_image_path"], self.root / "white.png")
        self.assertEqual(len(rows[0]["_pixel_hash"]), 64)
        self.assertEqual(rows[0]["data_kind"], "real")

    def test_group_and_decoded_image_split_leakage(self):
        self.write([self.row(split="train"), self.row("s2", "black.png", "test")])
        with self.assertRaisesRegex(DatasetError, "group leakage"):
            validate_manifest(self.manifest)
        # Different byte encodings of exactly the same image must still clash.
        Image.new("RGB", (160, 160), "white").save(self.root / "white-reencoded.png", compress_level=0)
        self.write([self.row(split="train"), self.row("s2", "white-reencoded.png", "test", "different")])
        with self.assertRaisesRegex(DatasetError, "image leakage"):
            validate_manifest(self.manifest)

    def test_multiple_identity_groups_cannot_cross_splits(self):
        a, b = self.row(split="train"), self.row("s2", "black.png", "test", "black")
        a["group_ids"], b["group_ids"] = ["person:one", "product:white"], ["person:one", "product:black"]
        self.write([a, b])
        with self.assertRaisesRegex(DatasetError, "group leakage"):
            validate_manifest(self.manifest)

    def test_rejects_paths_labels_missing_group_and_duplicate_ids(self):
        for image in ("../white.png", "C:\\private\\photo.png", "https://example.test/photo.png", "/tmp/photo.png", "missing.png"):
            with self.subTest(image=image):
                self.write([self.row(image=image)])
                with self.assertRaises(DatasetError):
                    validate_manifest(self.manifest)
        row = self.row()
        del row["group_id"]
        self.write([row])
        with self.assertRaises(DatasetError):
            validate_manifest(self.manifest)
        row = self.row()
        row["garments"][0]["color"] = "fuchsia"
        self.write([row])
        with self.assertRaises(DatasetError):
            validate_manifest(self.manifest)
        self.write([self.row(), self.row()])
        with self.assertRaisesRegex(DatasetError, "duplicate"):
            validate_manifest(self.manifest)

    def test_bad_json_field_types_produce_dataset_errors_without_tracebacks(self):
        for field, value in (("mode", []), ("mode", {}), ("data_kind", {}), ("data_kind", [])):
            with self.subTest(field=field, value=value):
                row = self.row()
                row[field] = value
                self.write([row])
                with self.assertRaises(DatasetError):
                    validate_manifest(self.manifest)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(main(["--manifest", str(self.manifest), "--validate-only"]), 2)
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_image_preprocessing_matches_live_dimensions(self):
        path = self.root / "large.png"
        Image.new("RGB", (1600, 800), "blue").save(path)
        with Image.open(io.BytesIO(normalized_image_bytes(path))) as image:
            self.assertEqual(image.size, (768, 384))
            self.assertEqual(image.format, "JPEG")

    def test_cli_offline_report_has_no_image_path_and_ignores_claimed_latency(self):
        row = self.row()
        row["data_kind"] = "smoke"
        self.write([row])
        guesses = self.root / "predictions.jsonl"
        guesses.write_text(json.dumps({"sample_id": "s1", "garments": [garment()], "latency_seconds": .001}), encoding="utf-8")
        output = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--manifest", str(self.manifest), "--predictions", str(guesses),
                         "--model", "test-only-model", "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(report["benchmark_eligible"])
        self.assertEqual(report["metrics"]["exact_match_accuracy"], 1)
        self.assertEqual(report["metrics"]["latency_seconds"]["count"], 0)
        contents = output.read_text(encoding="utf-8")
        for private in ("white.png", str(self.root), "base64", "_image_path", "_pixel_hash"):
            self.assertNotIn(private, contents)

    def test_cli_validate_only_never_needs_a_model(self):
        self.write([self.row()])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--manifest", str(self.manifest), "--validate-only"]), 0)
        self.assertTrue(json.loads(output.getvalue())["valid"])

    def test_cli_does_not_overwrite_inputs_and_rejects_empty_split(self):
        self.write([self.row()])
        original = self.manifest.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--manifest", str(self.manifest), "--output", str(self.manifest)]), 2)
            self.assertEqual(main(["--manifest", str(self.manifest), "--split", "validation",
                                   "--output", str(self.root / "report.json")]), 2)
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_cli_failures_exit_nonzero_but_write_an_honest_report(self):
        self.write([self.row()])
        guesses = self.root / "failed.jsonl"
        guesses.write_text(json.dumps({"sample_id": "s1", "error": "timeout"}), encoding="utf-8")
        output = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--manifest", str(self.manifest), "--predictions", str(guesses),
                         "--model", "test-model", "--output", str(output)])
        self.assertEqual(code, 1)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["metrics"]["failed_samples"], 1)
        self.assertEqual(report["metrics"]["category"]["recall"], 0)

    def test_cli_preserves_existing_report_without_explicit_overwrite(self):
        self.write([self.row()])
        guesses = self.root / "guesses.jsonl"
        guesses.write_text(json.dumps({"sample_id": "s1", "garments": [garment()]}), encoding="utf-8")
        output = self.root / "existing.json"
        output.write_text("preserve baseline", encoding="utf-8")
        arguments = ["--manifest", str(self.manifest), "--predictions", str(guesses),
                     "--model", "test-model", "--output", str(output)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments), 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "preserve baseline")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments + ["--overwrite"]), 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["metrics"]["sample_count"], 1)

    def test_fingerprint_tracks_pixels_labels_and_prompt_versions(self):
        self.write([self.row()])
        predictions = {"s1": {"garments": [garment()]}}
        first = build_report(self.manifest, validate_manifest(self.manifest), predictions,
                             metadata={"prompt_variant": "baseline"}, split="test")
        Image.new("RGB", (160, 160), "red").save(self.root / "white.png")
        second = build_report(self.manifest, validate_manifest(self.manifest), predictions,
                              metadata={"prompt_variant": "baseline"}, split="test")
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertNotEqual(first["selected_dataset_sha256"], second["selected_dataset_sha256"])
        self.assertEqual(first["experiment"]["prompt_version"], PROMPT_VERSION)
        for variant in ("baseline", "domain"):
            self.assertEqual(set(first["experiment"]["prompt_sha256"][variant]), {"person", "garment"})
            self.assertEqual(len(first["experiment"]["prompt_sha256"][variant]["person"]), 64)
        rows = validate_manifest(self.manifest)
        rows[0]["garments"][0]["color"] = "蓝"
        third = build_report(self.manifest, rows, predictions, metadata={}, split="test")
        self.assertNotEqual(second["selected_dataset_sha256"], third["selected_dataset_sha256"])

    def test_offline_prediction_failure_and_unknown_ids(self):
        guesses = self.root / "guesses.jsonl"
        guesses.write_text(json.dumps({"sample_id": "s1", "garments": [{"category": "invalid"}]}), encoding="utf-8")
        result = load_predictions(guesses, {"s1"})
        self.assertEqual(result["s1"]["error"], "invalid_prediction")
        with self.assertRaises(DatasetError):
            load_predictions(guesses, {"other"})


class MetricsTests(unittest.TestCase):
    def test_same_category_matching_is_order_independent(self):
        truth = [garment(color="白"), garment(color="黑", pattern="条纹")]
        result = score_predictions([sample(garments=truth)], {"s1": {"garments": truth[::-1]}})
        self.assertEqual(result["category"]["f1"], 1)
        self.assertEqual(result["attributes"]["color"]["macro_f1"], 1)
        self.assertEqual(result["exact_match_accuracy"], 1)

    def test_missing_extra_and_failed_calls_stay_in_denominators(self):
        rows = [sample("s1", [garment(), garment("bottoms", "黑")]), sample("s2")]
        guesses = {"s1": {"garments": [garment(), garment("shoes", "红")]}, "s2": {"error": "vision_timeout"}}
        result = score_predictions(rows, guesses)
        self.assertEqual(result["failed_samples"], 1)
        self.assertEqual(result["missed_garments"], 2)
        self.assertEqual(result["extra_garments"], 1)
        self.assertEqual(result["category"]["tp"], 1)
        self.assertEqual(result["category"]["fn"], 2)
        self.assertAlmostEqual(result["attributes"]["color"]["accuracy"], 1 / 3)
        self.assertEqual(result["exact_match_accuracy"], 0)

    def test_unknown_ground_truth_and_unknown_predictions_are_distinct(self):
        truth = [garment(color="未知", pattern="条纹")]
        guesses = [garment(color="蓝", pattern="未知")]
        result = score_predictions([sample(garments=truth)], {"s1": {"garments": guesses}})
        self.assertEqual(result["attributes"]["color"]["unsupported_attribute_rate_on_unknown_targets"], 1)
        self.assertEqual(result["attributes"]["pattern"]["unknown_output_rate_on_known_targets"], 1)
        self.assertEqual(result["attributes"]["fit"]["accuracy"], 1)

    def test_valid_empty_images_and_errors_are_not_equivalent(self):
        rows = [sample("empty", []), sample("failed", [])]
        result = score_predictions(rows, {"empty": {"garments": []}, "failed": {"error": "timeout"}})
        self.assertEqual(result["exact_match_accuracy"], .5)
        self.assertIsNone(result["category"]["f1"])
        self.assertEqual(result["failed_samples"], 1)

    def test_invalid_and_missing_predictions_count_as_failure(self):
        rows = [sample("bad"), sample("missing")]
        result = score_predictions(rows, {"bad": {"garments": [{"category": "script"}]}})
        self.assertEqual(result["failed_samples"], 2)
        self.assertEqual(result["category"]["recall"], 0)

    def test_real_model_uses_strict_observe_and_records_latency(self):
        class FakeVision:
            def observe(self, data, mode, strict=False):
                assert strict
                assert mode == "garment"
                raise StylingError("vision_timeout", "do not log private messages")
        with tempfile.TemporaryDirectory() as runtime:
            image = Path(runtime) / "photo.png"
            Image.new("RGB", (128, 128), "white").save(image)
            row = {**sample(), "_image_path": image}
            predictions = run_model([row], FakeVision())
        self.assertEqual(predictions["s1"]["error"], "vision_timeout")
        result = score_predictions([row], predictions)
        self.assertEqual(result["latency_seconds"]["count"], 1)
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
