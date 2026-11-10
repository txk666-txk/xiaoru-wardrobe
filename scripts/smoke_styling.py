"""Run real local vision and catalog planning using supplied public test images."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.styling.service import StylingService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=Path, default=Path(__file__).resolve().parents[2] / "fashn-vton-1.5/examples/data")
    args = parser.parse_args()
    report = []
    with tempfile.TemporaryDirectory(prefix="fay-style-smoke-") as root:
        service = StylingService(root=root)
        try:
            print(json.dumps(service.vision.status(), ensure_ascii=False), flush=True)
            for mode, name in [("person", "model.webp"), ("garment", "garment.webp")]:
                started = time.monotonic()
                with (args.examples / name).open("rb") as photo:
                    job = service.create("smoke", mode=mode, options={"scene": "日常", "budget": 400}, stream=photo, consent=True)
                print("Analyzing official reference:", mode, flush=True)
                while job["status"] in {"queued", "running"}:
                    if time.monotonic() - started > 300:
                        raise TimeoutError("Real vision did not finish")
                    time.sleep(1)
                    job = service.get("smoke", job["job_id"])
                if job["status"] != "completed":
                    raise RuntimeError(job["message"])
                result = job["result"]
                assert result["observation"]["source"] == "local_vision"
                assert all(outfit["total"] <= 400 for outfit in result["outfits"])
                assert service._jobs[job["job_id"]]["image"] is None
                row = {"mode": mode, "seconds": round(time.monotonic() - started, 1),
                       "observation": result["observation"], "outfits": len(result["outfits"]),
                       "totals": [outfit["total"] for outfit in result["outfits"]]}
                report.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                service.delete("smoke", job["job_id"])
        finally:
            service.close()
    print(json.dumps({"ok": True, "device": "CPU", "source": "Official FASHN example images", "cases": report}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
