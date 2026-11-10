"""AI 虚拟试衣 HTTP API；与本地演示交易共享同源会话和 CSRF 边界。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.formparser import parse_form_data

from agent.tryon import TryOnError, get_tryon_service
from agent.tryon.service import MAX_UPLOAD_BYTES
from gui.commerce_api import _ensure_session, _require_same_origin, _require_write_session, _set_session_cookie


MAX_TRYON_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024


def _error(exc: TryOnError):
    return jsonify({"ok": False, "error": exc.code, "message": str(exc)}), exc.status


def create_tryon_blueprint() -> Blueprint:
    blueprint = Blueprint("fay_tryon", __name__)

    @blueprint.errorhandler(TryOnError)
    def handle_tryon_error(exc: TryOnError):
        return _error(exc)

    @blueprint.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(exc: RequestEntityTooLarge):
        return _error(TryOnError("image_too_large", "上传请求过大，请使用不超过 64MB 的照片", 413))

    @blueprint.route("/api/tryon/status", methods=["GET"])
    @_require_same_origin
    def status():
        raw, _, _ = _ensure_session()
        response = jsonify({"ok": True, "data": get_tryon_service().status(request.args.get("product_id"))})
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/tryon/jobs", methods=["POST"])
    @_require_write_session
    def create_job(session_id: str):
        # 限制仅作用于试衣上传，不修改整个 Flask 应用的上传配置。
        # 为 multipart 边界和表单字段留出余量；单张照片仍精确限制 64MB。
        # 使用独立解析器也兼容 max_content_length 只读的 Flask 3.0。
        _, form, files = parse_form_data(
            request.environ,
            max_content_length=MAX_TRYON_REQUEST_BYTES,
            max_form_memory_size=500_000,
            max_form_parts=10,
        )
        try:
            image = files.get("photo")
            if image is None:
                raise TryOnError("image_required", "请选择一张全身照片")
            consent = str(form.get("consent") or "").lower() in {"true", "1", "yes"}
            data = get_tryon_service().create(
                session_id=session_id,
                product_id=form.get("product_id") or "",
                beautify=form.get("beautify") or "natural",
                consent=consent,
                upload_stream=image.stream,
            )
        finally:
            for _, image in files.items(multi=True):
                image.close()
        data["result_url"] = f"/api/tryon/jobs/{data['job_id']}/image"
        return jsonify({"ok": True, "data": data}), 201

    @blueprint.route("/api/tryon/jobs/<job_id>/image", methods=["GET"])
    @_require_same_origin
    def result_image(job_id: str):
        raw, session_id, _ = _ensure_session()
        job = get_tryon_service().get_job(session_id, job_id)
        response = send_file(
            job.result_path,
            mimetype="image/jpeg",
            as_attachment=request.args.get("download") == "1",
            download_name=f"fay-tryon-{job.product_id}.jpg",
            max_age=0,
        )
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _set_session_cookie(response, raw)

    @blueprint.route("/api/tryon/jobs/<job_id>", methods=["DELETE"])
    @_require_write_session
    def delete_job(session_id: str, job_id: str):
        get_tryon_service().delete(session_id, job_id)
        return jsonify({"ok": True, "data": {"deleted": True}})

    return blueprint
