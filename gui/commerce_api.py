"""公开商品快照与本地演示交易 API。"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Blueprint, jsonify, make_response, request

from agent.store.repository import CommerceRepositoryError
from agent.store.service import CommerceServiceError, get_commerce_service
from agent.store.state_machine import InvalidStateTransition


COOKIE_NAME = "fay_demo_store_session"
COOKIE_MAX_AGE = 24 * 60 * 60
_SESSION_SECRET = (os.environ.get("FAY_COMMERCE_SESSION_SECRET") or secrets.token_urlsafe(48)).encode(
    "utf-8"
)


def _error(code: str, message: str, status: int = 400):
    return jsonify({"ok": False, "error": code, "message": message}), status


def _raw_session_token() -> str | None:
    value = (request.cookies.get(COOKIE_NAME) or "").strip()
    if len(value) < 32 or len(value) > 256:
        return None
    return value


def _session_id(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def get_request_demo_session_id() -> str | None:
    """返回当前同源请求的本地演示会话 ID；不会创建新会话。"""
    if not _same_origin():
        return None
    raw = _raw_session_token()
    return _session_id(raw) if raw else None


def _csrf_token(raw_token: str) -> str:
    return hmac.new(_SESSION_SECRET, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def _same_origin() -> bool:
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme == request.scheme and parsed.netloc == request.host


def _set_session_cookie(response, raw_token: str):
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=request.is_secure,
        samesite="Strict",
        path="/api",
    )
    return response


def _ensure_session() -> tuple[str, str, bool]:
    raw = _raw_session_token()
    created = False
    if not raw:
        raw = secrets.token_urlsafe(32)
        created = True
    session_id = _session_id(raw)
    get_commerce_service().repository.ensure_session(session_id)
    return raw, session_id, created


def _require_write_session(view: Callable[..., Any]):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _same_origin():
            return _error("cross_origin_rejected", "演示交易接口只接受同源请求", 403)
        raw = _raw_session_token()
        if not raw:
            return _error("missing_demo_session", "请先初始化本地演示会话", 401)
        supplied = (request.headers.get("X-Fay-Demo-CSRF") or "").strip()
        if not supplied or not hmac.compare_digest(supplied, _csrf_token(raw)):
            return _error("csrf_rejected", "演示交易安全校验失败，请刷新页面后重试", 403)
        get_commerce_service().repository.ensure_session(_session_id(raw))
        return view(_session_id(raw), *args, **kwargs)

    return wrapped


def _require_same_origin(view: Callable[..., Any]):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _same_origin():
            return _error("cross_origin_rejected", "演示会话数据只接受同源请求", 403)
        return view(*args, **kwargs)

    return wrapped


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise CommerceServiceError("invalid_json", "请求体必须是 JSON 对象")
    return payload


def _integer(value: object, field: str, default: int | None = None) -> int:
    if value in (None, "") and default is not None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CommerceServiceError("invalid_integer", f"{field} 必须是整数") from exc


def create_commerce_blueprint() -> Blueprint:
    blueprint = Blueprint("fay_commerce", __name__)

    @blueprint.errorhandler(CommerceServiceError)
    @blueprint.errorhandler(CommerceRepositoryError)
    def handle_commerce_error(exc):
        status = 404 if exc.code.endswith("_not_found") else 409 if exc.code in {
            "cart_changed",
            "confirmation_used",
            "insufficient_balance",
            "order_not_cancellable",
            "order_cannot_advance",
            "return_not_available",
            "return_cannot_advance",
            "return_quantity_exceeded",
        } else 400
        return _error(exc.code, str(exc), status)

    @blueprint.errorhandler(InvalidStateTransition)
    def handle_transition_error(exc):
        return _error("invalid_state_transition", str(exc), 409)

    @blueprint.route("/api/store/session", methods=["GET"])
    @_require_same_origin
    def demo_session():
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify(
                {
                    "ok": True,
                    "data": {
                        "csrf_token": _csrf_token(raw),
                        "expires_in": COOKIE_MAX_AGE,
                        "demo_only": True,
                        "account": get_commerce_service().get_account(session_id),
                        "notice": "此会话使用独立沙盒钱包模拟下单、物流和退款，不连接真实支付或商家后台。",
                    },
                }
            )
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/account", methods=["GET"])
    @_require_same_origin
    def sandbox_account():
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().get_account(session_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/profile", methods=["GET"])
    def store_profile():
        return jsonify({"ok": True, "data": get_commerce_service().provider.get_store_profile()})

    @blueprint.route("/api/store/categories", methods=["GET"])
    def store_categories():
        return jsonify({"ok": True, "data": get_commerce_service().provider.list_categories()})

    @blueprint.route("/api/store/products", methods=["GET"])
    def products():
        try:
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 12))
        except ValueError:
            return _error("invalid_pagination", "page 和 page_size 必须是整数")
        data = get_commerce_service().list_products(
            query=request.args.get("q", ""),
            category=request.args.get("category"),
            page=page,
            page_size=page_size,
        )
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/products/<product_id>", methods=["GET"])
    def product_detail(product_id: str):
        return jsonify({"ok": True, "data": get_commerce_service().get_product(product_id)})

    @blueprint.route("/api/ecommerce/storefront", methods=["GET"])
    @_require_same_origin
    def storefront_compatibility():
        # username 仅为旧前端显示参数，不作为订单授权依据。
        raw, session_id, _ = _ensure_session()
        payload = get_commerce_service().storefront(session_id=session_id)
        response = make_response(jsonify({"ok": True, "data": payload}))
        return _set_session_cookie(response, raw)

    def _size_recommendation(session_id: str):
        payload = _json_body()
        data = get_commerce_service().recommend_size(
            product_id=str(payload.get("product_id") or ""),
            height_cm=payload.get("height_cm", payload.get("height")),
            weight_kg=payload.get("weight_kg", payload.get("weight")),
            chest_cm=payload.get("chest_cm", payload.get("chest")),
            shoulder_cm=payload.get("shoulder_cm", payload.get("shoulder")),
            waist_cm=payload.get("waist_cm", payload.get("waist")),
            hip_cm=payload.get("hip_cm", payload.get("hip")),
            fit_preference=payload.get("fit_preference", payload.get("fit")),
        )
        # session_id 只用于通过安全校验；身体参数不会写入数据库或日志。
        del session_id
        return jsonify({"ok": True, "data": data})

    blueprint.add_url_rule(
        "/api/store/size-recommendation",
        endpoint="size_recommendation",
        view_func=_require_write_session(_size_recommendation),
        methods=["POST"],
    )
    blueprint.add_url_rule(
        "/api/ecommerce/size-recommendation",
        endpoint="size_recommendation_compatibility",
        view_func=_require_write_session(_size_recommendation),
        methods=["POST"],
    )

    @blueprint.route("/api/store/cart", methods=["GET"])
    @_require_same_origin
    def get_cart():
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().get_cart(session_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/cart/items", methods=["POST"])
    @_require_write_session
    def set_cart_item(session_id: str):
        payload = _json_body()
        data = get_commerce_service().set_cart_item(
            session_id,
            str(payload.get("product_id") or ""),
            _integer(payload.get("quantity"), "quantity", 1),
            str(payload.get("sku_id") or "") or None,
        )
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/demo-orders/preview", methods=["POST"])
    @_require_write_session
    def preview_order(session_id: str):
        return jsonify({"ok": True, "data": get_commerce_service().preview_order(session_id)})

    @blueprint.route("/api/store/demo-orders", methods=["POST"])
    @_require_write_session
    def create_order(session_id: str):
        payload = _json_body()
        data = get_commerce_service().create_order(
            session_id,
            str(payload.get("confirmation_token") or ""),
            str(payload.get("idempotency_key") or ""),
        )
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/demo-orders", methods=["GET"])
    @_require_same_origin
    def list_orders():
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().list_orders(session_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/demo-orders/<order_id>", methods=["GET"])
    @_require_same_origin
    def get_order(order_id: str):
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().get_order(session_id, order_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/demo-orders/<order_id>/logistics", methods=["GET"])
    @_require_same_origin
    def get_order_logistics(order_id: str):
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().get_logistics(session_id, order_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/demo-orders/<order_id>/advance", methods=["POST"])
    @_require_write_session
    def advance_order(session_id: str, order_id: str):
        data = get_commerce_service().advance_order(session_id, order_id)
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/demo-orders/<order_id>/cancel", methods=["POST"])
    @_require_write_session
    def cancel_order(session_id: str, order_id: str):
        data = get_commerce_service().cancel_order(session_id, order_id)
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/demo-returns/preview", methods=["POST"])
    @_require_write_session
    def preview_return(session_id: str):
        payload = _json_body()
        data = get_commerce_service().preview_return(
            session_id,
            order_id=str(payload.get("order_id") or ""),
            order_item_id=_integer(payload.get("order_item_id"), "order_item_id"),
            quantity=_integer(payload.get("quantity"), "quantity"),
            reason_code=str(payload.get("reason_code") or ""),
        )
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/demo-returns", methods=["POST"])
    @_require_write_session
    def create_return(session_id: str):
        payload = _json_body()
        data = get_commerce_service().create_return(
            session_id,
            str(payload.get("confirmation_token") or ""),
            str(payload.get("idempotency_key") or ""),
        )
        return jsonify({"ok": True, "data": data})

    @blueprint.route("/api/store/demo-returns", methods=["GET"])
    @_require_same_origin
    def list_returns():
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().list_returns(session_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/demo-returns/<return_id>", methods=["GET"])
    @_require_same_origin
    def get_return(return_id: str):
        raw, session_id, _ = _ensure_session()
        response = make_response(
            jsonify({"ok": True, "data": get_commerce_service().get_return(session_id, return_id)})
        )
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/store/demo-returns/<return_id>/advance", methods=["POST"])
    @_require_write_session
    def advance_return(session_id: str, return_id: str):
        data = get_commerce_service().advance_return(session_id, return_id)
        return jsonify({"ok": True, "data": data})

    return blueprint
