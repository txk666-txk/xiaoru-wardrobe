"""Compare real local vector retrieval with the original keyword policy lookup.

This evaluates the retrieval tool, not answer quality or end-to-end routing.
The hand-labelled fixture is small; its result is not a production accuracy.
No chat API is called. A missing embedding model is a failed verification,
rather than a silently successful keyword fallback.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.policy_tool import PolicyTool


def first_policy(result):
    policies = (result.get("data") or {}).get("policies") or []
    return policies[0]["policy_id"] if policies else None


def summarize(rows, field):
    def counts(items):
        return {"correct": sum(row[field] == row["expected_policy_id"] for row in items),
                "total": len(items)}

    relevant = [row for row in rows if row["expected_policy_id"] is not None]
    unrelated = [row for row in rows if row["expected_policy_id"] is None]
    return {"overall": counts(rows), "policy_top1": counts(relevant),
            "out_of_scope_rejection": counts(unrelated)}


def run(split):
    fixture = PROJECT_ROOT / "test" / "fixtures" / "policy_rag_cases.json"
    raw = fixture.read_bytes()
    payload = json.loads(raw)
    cases = [case for case in payload["cases"] if split == "all" or case["split"] == split]
    if not cases:
        raise ValueError("No matching evaluation cases")

    baseline = PolicyTool(retriever=False)
    hybrid = PolicyTool()
    known_ids = {policy["policy_id"] for policy in baseline.policies}
    for case in cases:
        if case["expected_policy_id"] is not None and case["expected_policy_id"] not in known_ids:
            raise ValueError("Fixture refers to an unknown policy ID: " + case["id"])

    # Separately measure the first request: it imports and loads the encoder and
    # creates the corpus matrix. Do not mix that startup time into warm latency.
    started = time.perf_counter()
    warmup = hybrid.execute(query="七天无理由退货有哪些条件？", concise=True)
    cold_start_seconds = time.perf_counter() - started
    retrieval = (warmup.get("data") or {}).get("retrieval") or {}
    if retrieval.get("mode") != "hybrid":
        raise RuntimeError("Real vector retrieval unavailable; mode=" + str(retrieval.get("mode"))
                           + ". Run scripts/setup_policy_rag.py and retry. No vector result is claimed.")

    rows = []
    for case in cases:
        started = time.perf_counter()
        old_result = baseline.execute(query=case["query"], concise=True)
        keyword_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        new_result = hybrid.execute(query=case["query"], concise=True)
        hybrid_ms = (time.perf_counter() - started) * 1000
        metadata = (new_result.get("data") or {}).get("retrieval") or {}
        if metadata.get("mode") != "hybrid":
            raise RuntimeError("Vector retrieval fell back during case " + case["id"])
        row = {**case, "keyword_policy_id": first_policy(old_result),
               "hybrid_policy_id": first_policy(new_result),
               "keyword_ms": round(keyword_ms, 3), "hybrid_ms": round(hybrid_ms, 3),
               "retrieval": metadata}
        rows.append(row)
        status = "OK" if row["hybrid_policy_id"] == row["expected_policy_id"] else "MISS"
        print(f"{status:4s} {case['id']}: expected={case['expected_policy_id']} "
              f"keyword={row['keyword_policy_id']} hybrid={row['hybrid_policy_id']}", flush=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "PolicyTool retrieval only; no LLM calls, routing or generated-answer evaluation",
        "limitation": "Small manually labelled examples, not production accuracy. Split labels do not establish a blind benchmark.",
        "fixture_description": payload.get("description"),
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "split": split,
        "vector_verification": "hybrid required and observed for every evaluated case",
        "model": retrieval.get("model"),
        "index_fingerprint": retrieval.get("index_fingerprint"),
        "cold_start_seconds": round(cold_start_seconds, 3),
        "warm_hybrid_latency_ms": {
            "median": round(statistics.median(row["hybrid_ms"] for row in rows), 3),
            "mean": round(statistics.mean(row["hybrid_ms"] for row in rows), 3),
            "max": max(row["hybrid_ms"] for row in rows),
        },
        "keyword": summarize(rows, "keyword_policy_id"),
        "hybrid": summarize(rows, "hybrid_policy_id"),
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="test")
    parser.add_argument("--output", type=Path, help="Optional UTF-8 JSON report path")
    args = parser.parse_args()
    try:
        report = run(args.split)
    except (ValueError, RuntimeError) as exc:
        print("Verification failed: " + str(exc), file=sys.stderr)
        return 2
    for name in ("keyword", "hybrid"):
        metrics = report[name]
        print(name + ": " + ", ".join(
            f"{group}={value['correct']}/{value['total']}" for group, value in metrics.items()))
    print(f"Cold start: {report['cold_start_seconds']}s; warm retrieval latency: "
          + json.dumps(report["warm_hybrid_latency_ms"]) + " ms")
    print(report["limitation"])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("JSON report saved: " + str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
