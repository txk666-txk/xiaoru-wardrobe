from __future__ import annotations

import argparse
import configparser
import json
import math
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _embedding_endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/embeddings"):
        return url
    if re.search(r"/v\d+(?:\.\d+)?$", url, flags=re.IGNORECASE):
        return f"{url}/embeddings"
    return f"{url}/v1/embeddings"


def _load_embedding_config() -> tuple[str, str]:
    parser = configparser.ConfigParser()
    config_path = PROJECT_ROOT / "system.conf"
    if not parser.read(config_path, encoding="utf-8"):
        raise RuntimeError(f"无法读取配置文件: {config_path}")

    model = parser.get("key", "embedding_api_model", fallback="").strip()
    base_url = parser.get("key", "embedding_base_url", fallback="").strip()
    if not model or not base_url:
        raise RuntimeError("system.conf 缺少 embedding_api_model 或 embedding_base_url")
    return model, base_url


def _validate_local_endpoint(endpoint: str) -> None:
    host = (urlparse(endpoint).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"为避免误传记忆，迁移脚本只允许本地 Embedding 服务，当前地址: {endpoint}")


def _extract_vectors(payload: dict, expected_count: int) -> list[list[float]]:
    if isinstance(payload.get("data"), list):
        items = sorted(payload["data"], key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
    else:
        vectors = payload.get("embeddings")

    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise RuntimeError(
            f"Embedding 返回数量异常: 期望 {expected_count}，实际 "
            f"{len(vectors) if isinstance(vectors, list) else '未知'}"
        )

    checked: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise RuntimeError("Embedding 返回了空向量或无效向量")
        converted = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in converted):
            raise RuntimeError("Embedding 返回包含 NaN 或 Infinity")
        checked.append(converted)
    return checked


def migrate(username: str, batch_size: int) -> None:
    model, base_url = _load_embedding_config()
    endpoint = _embedding_endpoint(base_url)
    _validate_local_endpoint(endpoint)

    memory_dir = PROJECT_ROOT / "memory" / username / "memory_stream"
    nodes_path = memory_dir / "nodes.json"
    embeddings_path = memory_dir / "embeddings.json"
    nodes = json.loads(nodes_path.read_text(encoding="utf-8"))
    old_embeddings = json.loads(embeddings_path.read_text(encoding="utf-8"))

    contents: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        content = node.get("content") if isinstance(node, dict) else None
        if isinstance(content, str) and content and content not in seen:
            contents.append(content)
            seen.add(content)
    for content in old_embeddings:
        if isinstance(content, str) and content and content not in seen:
            contents.append(content)
            seen.add(content)

    if not contents:
        raise RuntimeError("没有找到可迁移的记忆内容")

    migrated: dict[str, list[float]] = {}
    expected_dim: int | None = None
    for offset in range(0, len(contents), batch_size):
        batch = contents[offset : offset + batch_size]
        response = requests.post(
            endpoint,
            json={"model": model, "input": batch},
            timeout=180,
        )
        response.raise_for_status()
        vectors = _extract_vectors(response.json(), len(batch))
        for content, vector in zip(batch, vectors):
            if expected_dim is None:
                expected_dim = len(vector)
            elif len(vector) != expected_dim:
                raise RuntimeError(
                    f"Embedding 维度不一致: 期望 {expected_dim}，实际 {len(vector)}"
                )
            migrated[content] = vector
        print(f"已生成 {min(offset + len(batch), len(contents))}/{len(contents)} 条向量")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = embeddings_path.with_name(f"embeddings.pre-{model.replace('/', '_')}.{timestamp}.json.bak")
    shutil.copy2(embeddings_path, backup_path)

    temporary_path = embeddings_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_path, embeddings_path)

    print(f"迁移完成: model={model}, dim={expected_dim}, entries={len(migrated)}")
    print(f"旧向量备份: {backup_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="把 Fay 旧记忆批量迁移到当前本地 Embedding 模型")
    parser.add_argument("--username", default="User")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size 必须大于 0")
    migrate(args.username, args.batch_size)


if __name__ == "__main__":
    main()
