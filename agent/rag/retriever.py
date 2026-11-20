"""Small, read-only policy index. No network download on the request path."""
from __future__ import annotations

import hashlib
import os
import re
import threading
from functools import lru_cache

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


def policy_chunks(policies):
    """Sentence-sized index chunks, linked back to complete answer policies.

    Retrieval can match a specific clause; answers always receive its complete
    parent policy so exceptions and qualification rules cannot be truncated.
    """
    return [dict(item, chunk_id=item["policy_id"] + ":" + str(i),
                 text=item["title"] + "。" + sentence)
            for item in policies
            for i, sentence in enumerate(re.split(r"(?<=[。；])", item["content"]))
            if len(sentence.strip()) >= 8]


class LocalEncoder:
    def __init__(self, model):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model, device="cpu", local_files_only=True)
        self.model.max_seq_length = 512
        self.lock = threading.Lock()

    def encode(self, texts, *, query=False):
        with self.lock:
            return self.model.encode(
                [QUERY_PREFIX + text if query else text for text in texts],
                normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
            )


@lru_cache(maxsize=2)
def local_encoder(model):
    return LocalEncoder(model)


class PolicyRetriever:
    """Cosine retrieval with a small keyword tie-breaker and evidence threshold.

    For nine short policies a normalized NumPy matrix is sufficient; no vector
    server is required. Explicit policy IDs remain outside this retriever.
    """
    def __init__(self, policies, *, encoder=None, threshold=0.52):
        self.chunks = policy_chunks(policies)
        self.policies = {p['policy_id']: p for p in policies}
        self.model_name = os.getenv("FAY_RAG_MODEL_PATH") or DEFAULT_MODEL
        self.encoder = encoder
        self.threshold = threshold
        self.vectors = None
        self.unavailable = False
        self.lock = threading.Lock()
        self.fingerprint = hashlib.sha256(
            "\n".join(p["text"] for p in self.chunks).encode("utf-8")
        ).hexdigest()[:16]

    def _prepare(self):
        if self.unavailable:
            return False
        with self.lock:
            if self.vectors is not None:
                return True
            try:
                self.encoder = self.encoder or local_encoder(self.model_name)
                self.vectors = self.encoder.encode([p["text"] for p in self.chunks])
                return True
            except Exception:
                # Missing model/dependency is a stable, inspectable fallback;
                # do not return provider exceptions or start network downloads.
                self.unavailable = True
                return False

    def search(self, query, *, keyword_scores=None, limit=2):
        if not self._prepare():
            return None, {"mode": "keyword_fallback", "reason": "local_embedding_unavailable"}
        try:
            import numpy as np
            vector = self.encoder.encode([str(query)[:1200]], query=True)[0]
            scores = np.asarray(self.vectors) @ vector
        except Exception:
            return None, {"mode": "keyword_fallback", "reason": "embedding_failed"}
        keywords = keyword_scores or {}
        ranked_by_policy = {}
        for chunk, score in zip(self.chunks, scores):
            cosine = float(score)
            # The bonus cannot lift a below-threshold semantic match into evidence.
            bonus = min(keywords.get(chunk["policy_id"], 0), 40) / 40 * 0.04
            item = (cosine + bonus, cosine, chunk)
            if item[0] > ranked_by_policy.get(chunk['policy_id'], (-1,))[0]:
                ranked_by_policy[chunk['policy_id']] = item
        ranked = list(ranked_by_policy.values())
        ranked.sort(key=lambda item: item[0], reverse=True)
        best = ranked[0][1] if ranked else 0.0
        matches = [(chunk, cosine) for _, cosine, chunk in ranked
                   if cosine >= self.threshold and cosine >= best - 0.08][:limit]
        metadata = {"mode": "hybrid", "model": self.model_name,
                    "index_fingerprint": self.fingerprint, "threshold": self.threshold,
                    "top_score": round(best, 4), "hits": [
                        {"chunk_id": p["chunk_id"], "policy_id": p["policy_id"],
                         "score": round(s, 4)} for p, s in matches]}
        # Return original policy fields only; chunk text is separate index metadata.
        return [self.policies[p['policy_id']] for p, _ in matches], metadata
