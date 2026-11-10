"""客服回复审核与基础 Guardrail。"""

from __future__ import annotations

import re


class ReflectionGuardrail:
    """在最终回复前修正过度承诺和不安全表达。"""

    _guarantee_patterns = (
        re.compile(r"(?:保证|确保|肯定|一定)明天(?:可以|会)?送达"),
        re.compile(r"明天(?:保证|肯定|一定)(?:可以|会)?送达"),
    )

    def review(self, answer: str, *, intent: str = "", evidence: dict | None = None) -> str:
        text = (answer or "").strip()
        if not text:
            return "抱歉，我暂时无法确认这项信息，请稍后重试或转接人工客服。"

        if intent == "logistics_query" or any(word in text for word in ("物流", "送达", "到货")):
            replacement = "根据当前物流信息，预计明天送达，实际时间请以物流后续更新为准"
            for pattern in self._guarantee_patterns:
                text = pattern.sub(replacement, text)

            text = re.sub(r"(?:保证|确保)按时送达", "预计按当前进度送达，实际时间请以物流更新为准", text)

        if any(word in text for word in ("退款已成功", "订单已取消", "地址已修改")):
            if not evidence or not evidence.get("write_operation_confirmed"):
                text = (
                    "当前演示版没有执行真实交易操作，我还不能确认操作已经完成。"
                    "如需继续，请核对订单信息并转接人工客服处理。"
                )

        return text
