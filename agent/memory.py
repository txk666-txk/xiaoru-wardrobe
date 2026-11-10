"""Agent Core 的轻量会话记忆。

这里仅保存客服任务继续执行所需的最少实体（如最近订单号），不保存完整地址、
手机号、支付信息。Fay 原有长期记忆仍由原模块负责，两者与企业知识库分离。
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class UserSession:
    recent_intents: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    last_order_id: str | None = None
    last_product_id: str | None = None
    pending_action: dict[str, str] | None = None


class SessionMemory:
    def __init__(self) -> None:
        self._sessions: dict[str, UserSession] = defaultdict(UserSession)
        self._lock = threading.RLock()

    def remember(
        self,
        user_id: str,
        intent: str,
        *,
        order_id: str | None = None,
        product_id: str | None = None,
    ) -> None:
        with self._lock:
            session = self._sessions[user_id]
            session.recent_intents.append(intent)
            if order_id:
                session.last_order_id = order_id
            if product_id:
                session.last_product_id = product_id

    def get_last_order_id(self, user_id: str) -> str | None:
        with self._lock:
            return self._sessions[user_id].last_order_id

    def get_last_product_id(self, user_id: str) -> str | None:
        with self._lock:
            return self._sessions[user_id].last_product_id

    def set_pending_action(self, user_id: str, action: dict[str, str] | None) -> None:
        with self._lock:
            self._sessions[user_id].pending_action = action

    def get_pending_action(self, user_id: str) -> dict[str, str] | None:
        with self._lock:
            pending = self._sessions[user_id].pending_action
            return dict(pending) if pending else None

    def clear(self, user_id: str | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._sessions.clear()
            else:
                self._sessions.pop(user_id, None)
