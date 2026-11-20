"""Download only the Chinese embedding model, explicitly and outside requests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.rag.retriever import DEFAULT_MODEL


def main():
    from huggingface_hub import snapshot_download
    path = snapshot_download(DEFAULT_MODEL, allow_patterns=[
        "config.json", "modules.json", "sentence_bert_config.json",
        "config_sentence_transformers.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "vocab.txt", "1_Pooling/config.json", "model.safetensors",
    ], max_workers=3)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(path, device="cpu", local_files_only=True)
    vectors = model.encode(["七日无理由退货"], normalize_embeddings=True)
    print(f"Local policy embedding ready: dimension={vectors.shape[1]}, device=cpu")
    print("Model cached outside the Git repository. Restart the assistant if it was already running.")


if __name__ == "__main__":
    main()
