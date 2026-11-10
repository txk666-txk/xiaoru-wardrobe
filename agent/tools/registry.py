from __future__ import annotations

from collections.abc import Callable

from .logistics_tool import LogisticsTool
from .order_tool import OrderTool
from .policy_tool import PolicyTool
from .product_tool import ProductTool
from .size_recommendation_tool import SizeRecommendationTool
from .styling_tool import StylingTool, OutfitPhotoTool


class ToolRegistry:
    """集中注册和执行 Stage 1 电商工具。"""

    def __init__(self) -> None:
        tools = [ProductTool(), SizeRecommendationTool(), StylingTool(), OutfitPhotoTool(), OrderTool(), LogisticsTool(), PolicyTool()]
        self._tools = {tool.name: tool for tool in tools}

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str):
        return self._tools.get(name)

    def execute(self, name: str, **kwargs) -> dict:
        tool = self.get(name)
        if tool is None:
            return {
                "ok": False,
                "message": f"未知工具：{name}",
                "data": None,
                "error": "unknown_tool",
            }
        try:
            return tool.execute(**kwargs)
        except Exception as exc:
            return {
                "ok": False,
                "message": "工具执行失败，请稍后重试或转接人工客服。",
                "data": None,
                "error": str(exc),
            }
