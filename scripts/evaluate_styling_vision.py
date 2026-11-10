"""Evaluate a labelled clothing-image manifest, without reusing chat fixtures.

--validate-only checks all splits and makes no model requests.
--predictions scores saved JSONL model predictions without model requests.
Otherwise the selected local vision backend is called with strict validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.styling.vision_evaluation import (DatasetError, SPLITS, build_report,
    load_predictions, manifest_summary, run_model, validate_manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--split", choices=SPLITS, default="test")
    parser.add_argument("--output", type=Path, help="Local JSON report; never contains image bytes or image paths")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly allow replacing an existing report")
    parser.add_argument("--prompt-variant", choices=("baseline", "domain"), default="domain")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--predictions", type=Path, help="JSONL records: sample_id + garments, or sample_id + error")
    parser.add_argument("--backend", choices=("ollama", "openai_local"))
    parser.add_argument("--origin", help="Loopback-only model service address")
    parser.add_argument("--model", help="Explicit model ID for real calls; declared ID for offline predictions")
    args = parser.parse_args(argv)
    try:
        samples = validate_manifest(args.manifest)
        if args.validate_only:
            print(json.dumps({"valid": True, **manifest_summary(samples)}, ensure_ascii=False, indent=2))
            return 0
        if args.output is None:
            raise DatasetError("--output is required unless --validate-only is used")
        output_path = args.output.resolve()
        input_paths = {args.manifest.resolve(), *(sample["_image_path"] for sample in samples)}
        if args.predictions:
            input_paths.add(args.predictions.resolve())
        if output_path in input_paths:
            raise DatasetError("--output must not overwrite a manifest, image or prediction input")
        if args.output.exists() and not args.overwrite:
            raise DatasetError("Report already exists; choose a different --output or pass --overwrite")
        selected = [sample for sample in samples if sample["split"] == args.split]
        if not selected:
            raise DatasetError("The selected split contains no samples")
        if args.predictions:
            if not args.model:
                raise DatasetError("--model is required to identify offline predictions")
            predictions = load_predictions(args.predictions, {sample["sample_id"] for sample in selected})
            metadata = {"mode": "offline_predictions", "backend": args.backend or "declared_offline",
                        "model": args.model, "prompt_variant": args.prompt_variant,
                        "provenance": "model and prompt are declarations; no model was invoked"}
        else:
            from agent.styling.vision import LocalVision

            vision = LocalVision(backend=args.backend, origin=args.origin, model=args.model,
                                 prompt_variant=args.prompt_variant)
            predictions = run_model(selected, vision)
            metadata = {"mode": "real_model", "backend": vision.backend, "model": vision.model,
                        "prompt_variant": vision.prompt_variant}
        report = build_report(args.manifest, selected, predictions, metadata=metadata, split=args.split)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # JSON serialisation deliberately operates only on the redacted report,
        # never on the internal records containing _image_path and _pixel_hash.
        with args.output.open("w" if args.overwrite else "x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        print(json.dumps({"report": str(args.output), "samples": len(selected),
                          "failed_samples": report["metrics"]["failed_samples"],
                          "benchmark_eligible": report["benchmark_eligible"]}, ensure_ascii=False))
        return 1 if report["metrics"]["failed_samples"] else 0
    except (DatasetError, OSError, ValueError) as exc:
        # DatasetError messages carry no local absolute image path or labels.
        print("Vision evaluation failed: " + (str(exc) if isinstance(exc, DatasetError) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
