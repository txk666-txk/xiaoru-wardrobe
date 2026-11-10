"""SFT exports must keep holdout data and customer photos out of training output."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from agent.styling.vision_evaluation import DatasetError
from scripts.prepare_styling_vision_sft import prepare_dataset


class VisionSFTTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "manifest.jsonl"
        self.output = self.root / "export"

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        result = []
        for i, split in enumerate(("train", "validation", "test")):
            path = self.root / (split + ".png")
            Image.new("RGB", (160, 160), (50 + 40 * i, 20, 30)).save(path)
            result.append({"sample_id": split + "-1", "image": path.name, "mode": "garment",
                           "split": split, "group_id": split + "-product",
                           "garments": [{"category": "tops", "color": "白", "pattern": "条纹", "fit": "宽松"}]})
        return result

    def write_manifest(self, rows):
        self.manifest.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def test_sharegpt_format_and_holdout_not_exported(self):
        rows = self.rows()
        rows[0]["arbitrary_private_field"] = "must-not-export"
        self.write_manifest(rows)
        summary = prepare_dataset(self.manifest, self.output)
        train = json.loads((self.output / "train.json").read_text(encoding="utf-8"))
        validation = json.loads((self.output / "validation.json").read_text(encoding="utf-8"))
        self.assertEqual(len(train), 1)
        self.assertEqual(len(validation), 1)
        self.assertEqual(train[0]["conversations"][0]["from"], "human")
        self.assertEqual(train[0]["conversations"][0]["value"].count("<image>"), 1)
        self.assertEqual(train[0]["conversations"][1]["from"], "gpt")
        target = json.loads(train[0]["conversations"][1]["value"])
        self.assertEqual(target["garments"][0]["style"], "未知")
        self.assertTrue(Path(train[0]["images"][0]).is_absolute())
        self.assertFalse((self.output / "test.json").exists())
        all_outputs = "\n".join(p.read_text(encoding="utf-8") for p in self.output.iterdir())
        self.assertNotIn("test.png", all_outputs)
        self.assertNotIn("must-not-export", all_outputs)
        self.assertFalse(summary["training_executed"])
        self.assertTrue(summary["prompt_version"])
        self.assertEqual(summary["live_image_contract"]["max_edge_px"], 768)
        self.assertEqual(summary["split_counts"]["test"], 1)
        info = json.loads((self.output / "dataset_info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["fay_styling_vision_train"]["columns"],
                         {"messages": "conversations", "images": "images"})

    def test_existing_output_is_never_overwritten(self):
        self.write_manifest(self.rows())
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        with self.assertRaises(DatasetError):
            prepare_dataset(self.manifest, self.output)
        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertFalse((self.output / "train.json").exists())

    def test_group_leakage_rejected_before_any_output(self):
        rows = self.rows()
        rows[1]["group_id"] = rows[0]["group_id"]
        self.write_manifest(rows)
        with self.assertRaises(DatasetError):
            prepare_dataset(self.manifest, self.output)
        self.assertFalse(self.output.exists())

    def test_smoke_data_cannot_be_presented_as_real_sft(self):
        rows = self.rows()
        rows[0]["data_kind"] = "smoke"
        self.write_manifest(rows)
        with self.assertRaises(DatasetError):
            prepare_dataset(self.manifest, self.output)
        self.assertFalse(self.output.exists())

    def test_no_train_does_not_promote_holdout(self):
        self.write_manifest(self.rows()[1:])
        with self.assertRaises(DatasetError):
            prepare_dataset(self.manifest, self.output)
        self.assertFalse(self.output.exists())

    def test_no_validation_or_test_is_explicitly_reported(self):
        self.write_manifest(self.rows()[:1])
        summary = prepare_dataset(self.manifest, self.output)
        self.assertEqual(len(summary["warnings"]), 2)
        self.assertFalse((self.output / "validation.json").exists())
        self.assertEqual(summary["exported_splits"], ["train"])

    def test_invalid_label_rejected_before_export(self):
        rows = self.rows()
        rows[0]["garments"][0]["color"] = "编造颜色"
        self.write_manifest(rows)
        with self.assertRaises(DatasetError):
            prepare_dataset(self.manifest, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
