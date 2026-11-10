"""Agent Core 与 Fay 语音/数字人链路之间的薄适配层。"""

from __future__ import annotations

import threading
import re

from .controller import AgentController, AgentResponse


_controller: AgentController | None = None
_controller_lock = threading.Lock()


def redact_conversation_text(text, username='User', demo_session_id=None):
    from .privacy import redact_body_measurements
    from .styling.service import get_styling_service
    service = get_styling_service()
    owner = demo_session_id or service.voice_owner(username) or f'voice:{username}'
    state = service.conversation(owner) or {}
    from .styling.engine import normalize_styling_text
    if 'profile' in state.get('waiting', []) and re.fullmatch(r'\s*\d{3}(?:\.\d+)?\s*[。！]?\s*', normalize_styling_text(text)):
        return '[本次身高回答已脱敏]'
    return redact_body_measurements(normalize_styling_text(text))


def get_controller() -> AgentController:
    global _controller
    if _controller is None:
        with _controller_lock:
            if _controller is None:
                from .styling.planner import configured_planner
                _controller = AgentController(styling_planner=configured_planner())
    return _controller


def handle_text(
    text: str,
    username: str = "User",
    demo_session_id: str | None = None,
    product_id: str | None = None,
    attachment_id: str | None = None,
    voice: bool = False,
    conversation_id: str | None = None,
) -> AgentResponse:
    if voice and not demo_session_id:
        from .styling.service import get_styling_service
        demo_session_id = get_styling_service().voice_owner(username)
    context = {key: value for key, value in {'attachment_id': attachment_id, 'conversation_id': conversation_id}.items()
               if value is not None}
    return get_controller().handle(
        text,
        user_id=username or "User",
        demo_session_id=demo_session_id,
        product_id=product_id,
        **context,
    )
