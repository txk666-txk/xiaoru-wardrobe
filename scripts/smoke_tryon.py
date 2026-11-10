"""Exercise Fay's actual upload/result/delete API with a supplied test photo.

Run only against a local Fay instance. This calls the configured image model;
it is an integration check, not a unit test or a placeholder generator.
"""

import argparse
import io
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--person-image", type=Path, required=True)
    parser.add_argument("--product-id", help="Defaults to the first try-on-enabled catalog item")
    parser.add_argument("--output", type=Path, default=Path("cache_data/fashn-fay-smoke.jpg"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if urlparse(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Use a local Fay server for this test")

    session = requests.Session()
    session.trust_env = False
    response = session.get(f"{base}/api/security/session", timeout=10)
    response.raise_for_status()
    workbench_csrf = response.json()["csrf"]
    response = session.get(f"{base}/api/store/session", timeout=10)
    response.raise_for_status()
    headers = {
        "Origin": base,
        "X-Fay-CSRF": workbench_csrf,
        "X-Fay-Demo-CSRF": response.json()["data"]["csrf_token"],
    }
    product_id = args.product_id
    if not product_id:
        response = session.get(f"{base}/api/store/products", params={"page_size": 100}, timeout=10)
        response.raise_for_status()
        product_id = next(
            (item["product_id"] for item in response.json()["data"]["items"] if item.get("tryon_supported")),
            None,
        )
        if not product_id:
            raise RuntimeError("The catalog has no try-on-enabled garment with a real photo")
    response = session.get(f"{base}/api/tryon/status", timeout=10)
    response.raise_for_status()
    status = response.json()["data"]
    if not status["configured"] or status["development_placeholder"]:
        raise RuntimeError("A real try-on provider must be configured")
    print("Provider:", status["provider"])
    print("Product:", product_id, flush=True)
    started = time.monotonic()
    with args.person_image.open("rb") as photo:
        response = session.post(
            f"{base}/api/tryon/jobs",
            headers=headers,
            data={"product_id": product_id, "beautify": "natural", "consent": "true"},
            files={"photo": (args.person_image.name, photo, mimetypes.guess_type(args.person_image.name)[0] or "application/octet-stream")},
            timeout=660,
        )
    if response.status_code != 201:
        raise RuntimeError(f"Try-on failed: HTTP {response.status_code}: {response.text[:500]}")
    job = response.json()["data"]
    result_url = f"{base}/api/tryon/jobs/{job['job_id']}/image"
    try:
        response = session.get(result_url, timeout=10)
        response.raise_for_status()
        with Image.open(io.BytesIO(response.content)) as result:
            result.verify()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(response.content)
        print("Result:", args.output.resolve())
        print("Elapsed seconds:", round(time.monotonic() - started, 1))
    finally:
        response = session.delete(f"{base}/api/tryon/jobs/{job['job_id']}", headers=headers, timeout=10)
        response.raise_for_status()
    response = session.get(result_url, timeout=10)
    if response.status_code != 404:
        raise RuntimeError("Deleted result is still accessible")
    print("Upload, generation, download and deletion passed.")


if __name__ == "__main__":
    main()
