from __future__ import annotations

import re

from .common import load_json, tool_result


class PolicyTool:
    name = "query_policy"
    description = "查询退换货、质量售后、退款、优惠、隐私等政策"

    def __init__(self) -> None:
        payload = load_json("policies.json")
        self.dataset_label = payload.get("dataset_label", "演示店铺政策")
        self.policies = payload.get("policies", [])
        self.sources = {item["source_id"]: item for item in payload.get("sources", [])}

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [token for token in re.split(r"[\s，。！？、；：,.!?;:]+", text.lower()) if token]

    def _score(self, policy: dict, query: str) -> int:
        query_lower = query.lower()
        score = 0
        for keyword in policy.get("keywords", []):
            if keyword.lower() in query_lower:
                score += 10 + len(keyword)
        for token in self._tokens(query):
            if len(token) >= 2 and token in (policy.get("title", "") + policy.get("content", "")).lower():
                score += 1
        return score

    def execute(self, *, query: str = "", policy_id: str | None = None, concise: bool = False, **_: object) -> dict:
        if policy_id:
            matches = [item for item in self.policies if item.get("policy_id") == policy_id]
        else:
            scored = sorted(
                ((self._score(item, query), item) for item in self.policies),
                key=lambda pair: pair[0],
                reverse=True,
            )
            matches = [item for score, item in scored if score > 0][:1 if concise else 2]

        if not matches:
            return tool_result(
                ok=False,
                error="policy_not_found",
                message="我暂时没有检索到对应政策。你可以补充说明是退货、质量问题、退款到账、优惠活动还是隐私问题。",
            )

        sections = []
        source_items = []
        for policy in matches:
            scope = policy.get("scope", "")
            prefix = f"【{policy['title']}】"
            if scope == "demo_store_policy":
                prefix += "（演示店铺政策）"
            content = (policy.get("customer_reply") or policy["content"]) if concise else policy["content"]
            sections.append(prefix + content)
            for source_id in policy.get("source_ids", []):
                source = self.sources.get(source_id)
                if source and source not in source_items:
                    source_items.append(source)

        message = "\n".join(sections)
        if source_items:
            message += "\n适用时请以现行法律法规及店铺页面公示规则为准。"
        return tool_result(
            ok=True,
            message=message,
            data={"policies": matches, "sources": source_items},
        )
