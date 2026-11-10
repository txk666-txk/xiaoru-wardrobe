from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProductProvider(ABC):
    """商品目录读取协议。

    Provider 返回标准化 dict，方便后续替换为获得授权的真实电商平台实现。
    """

    @abstractmethod
    def get_store_profile(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def list_categories(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def search_products(
        self,
        *,
        query: str = "",
        category: str | None = None,
        page: int = 1,
        page_size: int = 12,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_product(self, product_id: str) -> dict[str, Any] | None:
        raise NotImplementedError
