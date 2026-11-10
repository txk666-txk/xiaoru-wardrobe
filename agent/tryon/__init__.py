"""AI 虚拟试衣服务。

默认不启用任何外部图像模型。只有显式配置 provider 后，照片才会交给对应实现处理。
"""

from .service import TryOnError, TryOnService, get_tryon_service

__all__ = ["TryOnError", "TryOnService", "get_tryon_service"]
