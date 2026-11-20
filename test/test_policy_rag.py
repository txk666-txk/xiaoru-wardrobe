"""Focused policy RAG tests that never require a model download or API key."""
import json
import os
import unittest
from unittest.mock import Mock, patch

import numpy as np
import requests

from agent.rag.answer import PolicyAnswerer
from agent.rag.retriever import PolicyRetriever
from agent.tools.policy_tool import PolicyTool


POLICIES = [
    {"policy_id": "return", "title": "退货", "content": "七日内可以申请，但法定例外除外。",
     "scope": "legal_baseline", "source_ids": ["law"]},
    {"policy_id": "shipping", "title": "发货", "content": "预计发货时间仅供参考。",
     "scope": "demo_store_policy", "source_ids": []},
]


class Encoder:
    def __init__(self, query):
        self.query = np.asarray(query, dtype=float)

    def encode(self, texts, *, query=False):
        return np.asarray([self.query] if query else [[1.0, 0.0], [0.0, 1.0]])


class PolicyRetrievalTests(unittest.TestCase):
    def test_vector_sorting_and_threshold(self):
        retriever = PolicyRetriever(POLICIES, encoder=Encoder([0.9, 0.2]))
        results, metadata = retriever.search("什么时候可以退货")
        self.assertEqual([p["policy_id"] for p in results], ["return"])
        self.assertEqual(metadata["mode"], "hybrid")
        self.assertEqual(metadata["hits"][0]["policy_id"], "return")
        self.assertNotIn("text", results[0])

    def test_unrelated_query_does_not_become_evidence_via_keyword_bonus(self):
        retriever = PolicyRetriever(POLICIES, encoder=Encoder([0.2, 0.3]))
        results, metadata = retriever.search("完全无关的问题", keyword_scores={"return": 100})
        self.assertEqual(results, [])
        self.assertEqual(metadata["hits"], [])

    def test_encoder_failure_requests_keyword_fallback(self):
        encoder = Mock()
        encoder.encode.side_effect = RuntimeError("model unavailable")
        results, metadata = PolicyRetriever(POLICIES, encoder=encoder).search("退货")
        self.assertIsNone(results)
        self.assertEqual(metadata["mode"], "keyword_fallback")

    def test_explicit_id_bypasses_search(self):
        retriever = Mock()
        result = PolicyTool(retriever=retriever).execute(policy_id="seven_day_no_reason")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["retrieval"]["mode"], "explicit_id")
        self.assertTrue(result["data"]["sources"])
        retriever.search.assert_not_called()

    def test_keyword_fallback_retains_policy_sources(self):
        retriever = Mock()
        retriever.search.return_value = (None, {"mode": "keyword_fallback"})
        result = PolicyTool(retriever=retriever).execute(query="七天无理由")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["policies"][0]["scope"], "legal_baseline")
        self.assertTrue(result["data"]["sources"])
        self.assertEqual(result["data"]["retrieval"]["mode"], "keyword_fallback")


class PolicyAnswerTests(unittest.TestCase):
    def setUp(self):
        self.result = {"ok": True, "message": "可信的原始回复", "data": {"policies": POLICIES}}
        self.generator = Mock()
        self.answerer = PolicyAnswerer(generator=self.generator)

    @patch.dict(os.environ, {"FAY_RAG_GENERATION": "model"})
    def test_valid_selection_renders_trusted_complete_policy(self):
        self.generator.choose.return_value = json.dumps({"sentence_ids": ["return:0", "shipping:0"]})
        text, info = self.answerer.answer("退货和发货规则？", self.result)
        self.assertIn("法定例外除外", text)
        self.assertIn("演示店铺政策", text)
        self.assertEqual(info["mode"], "model_grounded")
        self.assertEqual([c["policy_id"] for c in info["citations"]], ["return", "shipping"])

    @patch.dict(os.environ, {"FAY_RAG_GENERATION": "model"})
    def test_invalid_json_hallucinated_id_or_missing_primary_fall_back(self):
        for output in ("not json", '{"sentence_ids":["fake:0"]}',
                       '{"sentence_ids":["shipping:0"]}'):
            with self.subTest(output=output):
                self.generator.choose.return_value = output
                text, info = self.answerer.answer("退货规则？", self.result)
                self.assertEqual(text, self.result["message"])
                self.assertEqual(info["mode"], "extractive_fallback")

    @patch.dict(os.environ, {"FAY_RAG_GENERATION": "model"})
    def test_model_rejecting_all_evidence_asks_for_clarification(self):
        self.generator.choose.return_value = '{"sentence_ids":[]}'
        text, info = self.answerer.answer("不相关的问题", self.result)
        self.assertIn("补充", text)
        self.assertEqual(info["mode"], "no_relevant_evidence")
        self.assertEqual(info["citations"], [])

    @patch.dict(os.environ, {"FAY_RAG_GENERATION": "model"})
    def test_timeout_falls_back(self):
        self.generator.choose.side_effect = requests.Timeout()
        text, info = self.answerer.answer("退货规则？", self.result)
        self.assertEqual(text, self.result["message"])
        self.assertEqual(info["reason"], "provider_timeout")

    def test_no_evidence_does_not_call_model(self):
        result = {"ok": False, "message": "未找到", "data": {"policies": []}}
        text, info = self.answerer.answer("其他问题", result)
        self.assertEqual(text, "未找到")
        self.assertEqual(info["mode"], "no_evidence")
        self.generator.choose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
