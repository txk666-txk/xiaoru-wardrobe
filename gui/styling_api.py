"""Same-origin, session-scoped visual styling endpoints."""
import json
import io

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.formparser import parse_form_data

from agent.styling.service import MAX_UPLOAD, get_styling_service
from agent.styling.vision import StylingError
from agent.store.service import CommerceServiceError
from .commerce_api import _require_same_origin, _require_write_session, get_request_demo_session_id


def create_styling_blueprint():
    bp = Blueprint("fay_styling", __name__)

    @bp.errorhandler(StylingError)
    def error(exc):
        return jsonify(ok=False, error=exc.code, message=str(exc)), exc.status

    @bp.errorhandler(CommerceServiceError)
    def store_error(exc):
        return jsonify(ok=False, error=exc.code, message=str(exc)), 400

    @bp.errorhandler(RequestEntityTooLarge)
    def too_large(_):
        return jsonify(ok=False, error="image_too_large", message="照片不能超过64MB"), 413

    def owner():
        value = get_request_demo_session_id()
        if not value:
            raise StylingError("session_required", "请刷新页面初始化会话", 401)
        return value

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise StylingError("invalid_json", "请求必须为JSON对象")
        return data

    @bp.after_request
    def private(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.get("/api/styling/status")
    @_require_same_origin
    def status():
        return jsonify(ok=True, data=get_styling_service().vision.status())

    @bp.route("/api/styling/preferences", methods=["GET", "POST", "DELETE"])
    @_require_same_origin
    def preferences():
        if request.method == "GET":
            return jsonify(ok=True, data=get_styling_service().preferences.get(owner()))
        return write_preferences()

    @_require_write_session
    def write_preferences(session_id):
        store = get_styling_service().preferences
        if request.method == "DELETE":
            store.delete(session_id)
            return jsonify(ok=True, data={})
        data = body()
        return jsonify(ok=True, data=store.save(session_id, data.get("preferences", {}), data.get("consent")))

    @bp.post("/api/styling/jobs")
    @_require_write_session
    def create(session_id):
        service = get_styling_service()
        if request.is_json:
            data = body()
            job = service.create(session_id, mode="text", options=data.get("options"))
        else:
            _, form, files = parse_form_data(request.environ, max_content_length=MAX_UPLOAD + 1024 * 1024,
                                             max_form_memory_size=100_000, max_form_parts=6,
                                             stream_factory=lambda *args, **kwargs: io.BytesIO())
            try:
                try:
                    options = json.loads(form.get("options", "{}"))
                except (TypeError, ValueError):
                    raise StylingError("invalid_options", "穿搭参数无法解析")
                photo = files.get("photo")
                job = service.create(session_id, mode=form.get("mode", "person"), options=options,
                                     stream=photo.stream if photo else None, consent=form.get("consent") == "true")
            finally:
                for _, file in files.items(multi=True):
                    file.close()
        return jsonify(ok=True, data=job), 202

    @bp.get("/api/styling/jobs/<key>")
    @_require_same_origin
    def get(key):
        return jsonify(ok=True, data=get_styling_service().get(owner(), key))

    @bp.get("/api/styling/latest")
    @_require_same_origin
    def latest():
        return jsonify(ok=True, data=get_styling_service().latest(owner()))

    @bp.get('/api/styling/conversation')
    @_require_same_origin
    def conversation():
        state = get_styling_service().conversation(owner()) or {}
        return jsonify(ok=True, data=state.get('presentation'))

    @bp.post('/api/styling/voice')
    @_require_write_session
    def voice(session_id):
        # Only the physical microphone on this computer can share its browser conversation.
        if request.remote_addr not in {'127.0.0.1', '::1'}:
            raise StylingError('local_voice_only', '本机麦克风关联请在这台电脑的浏览器操作', 403)
        data = body()
        username = data.get('username')
        if not isinstance(username, str) or not username.strip() or len(username) > 80:
            raise StylingError('invalid_user', '请选择有效客户')
        get_styling_service().bind_voice(session_id, username, data.get('enabled') is True)
        return jsonify(ok=True, data={'linked': data.get('enabled') is True})

    @bp.post('/api/styling/attachments')
    @_require_write_session
    def attachment(session_id):
        _, form, files = parse_form_data(request.environ, max_content_length=MAX_UPLOAD + 1024 * 1024,
                                         max_form_memory_size=100_000, max_form_parts=4,
                                         stream_factory=lambda *args, **kwargs: io.BytesIO())
        try:
            if form.get('mode', 'person') not in {'person', 'garment'}:
                raise StylingError('invalid_mode', '对话附件请选择本人穿着照片或已有衣服')
            photo = files.get('photo')
            job = get_styling_service().create(session_id, mode=form.get('mode', 'person'),
                stream=photo.stream if photo else None, consent=form.get('consent') == 'true', defer=True)
            return jsonify(ok=True, data=job), 201
        finally:
            for _, file in files.items(multi=True):
                file.close()

    @bp.delete("/api/styling/jobs/<key>")
    @_require_write_session
    def delete(session_id, key):
        get_styling_service().delete(session_id, key)
        return jsonify(ok=True, data={})

    @bp.post("/api/styling/jobs/<key>/refine")
    @_require_write_session
    def refine(session_id, key):
        return jsonify(ok=True, data=get_styling_service().refine(session_id, key, body()))

    @bp.post("/api/styling/compare")
    @_require_write_session
    def compare(session_id):
        data = body()
        return jsonify(ok=True, data=get_styling_service().engine.compare(data.get("product_ids"), data.get("options")))

    return bp
