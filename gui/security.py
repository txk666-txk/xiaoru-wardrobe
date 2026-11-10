"""Default-deny remote access, CSRF and safe local resource paths."""
from __future__ import annotations

import hmac
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from flask import g, jsonify, request

from core.browser_security import authority, valid_username

COOKIE = 'fay_workbench_session'
PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_USER_ENDPOINTS = {
    '/api/send', '/api/system-status', '/api/to-greet', '/api/to-wake', '/api/to-stop-talking',
    '/api/execution-status', '/api/execution-cancel', '/api/execution-modify', '/transparent-pass',
}


def load_workbench_users():
    try:
        with (PROJECT / 'verifier.json').open(encoding='utf-8-sig') as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None


def safe_user_directory(username, root=None):
    if not valid_username(username):
        raise ValueError('无效用户名：不允许路径、盘符或目录跳转')
    base = Path(root or PROJECT / 'memory').resolve()
    candidate = (base / username).resolve()
    if candidate.parent != base:
        raise ValueError('用户记忆目录超出允许范围')
    return candidate


def public_image(path):
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('无效图片路径')
    candidate = Path(path).resolve()
    roots = (PROJECT / 'gui' / 'static', PROJECT / 'cache_data', PROJECT / 'samples')
    if not any(candidate.is_relative_to(root.resolve()) for root in roots):
        raise ValueError('仅允许访问项目公开图片目录')
    if candidate.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}:
        raise ValueError('不是有效的图片文件')
    return str(candidate)


def require_chat_user(username):
    if not valid_username(username):
        return jsonify({'success': False, 'message': '无效用户名'}), 400
    if not authority.claim(g.fay_browser_session, username):
        return jsonify({'success': False, 'message': '聊天身份由另一个浏览器占用，请切换用户'}), 403
    return None


def install_security(app, users_loader):
    @app.before_request
    def protect_workbench():
        # Do not trust forwarded headers: this workbench is intentionally local-only.
        try:
            local = ipaddress.ip_address(request.remote_addr or '').is_loopback
        except ValueError:
            local = False
        if not local or urlsplit(request.host_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
            return jsonify({'message': '工作台仅允许本机访问'}), 403
        origin = request.headers.get('Origin')
        if (origin and origin != request.host_url.rstrip('/')) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify({'message': '拒绝跨站请求'}), 403
        users = users_loader()
        if not isinstance(users, dict):
            return jsonify({'message': '工作台登录配置无效，请检查 verifier.json'}), 503
        if users:
            credentials = request.authorization
            expected = users.get(credentials.username) if credentials and credentials.type.lower() == 'basic' else None
            if not isinstance(expected, str) or not hmac.compare_digest(expected.encode(), (credentials.password or '').encode()):
                return jsonify({'message': '需要工作台登录'}), 401, {'WWW-Authenticate': 'Basic realm="Fay workbench"'}
        token, state = authority.session(request.cookies.get(COOKIE))
        g.fay_browser_session = token
        g.fay_csrf = state['csrf']
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            supplied = request.headers.get('X-Fay-CSRF', '')
            if not hmac.compare_digest(supplied.encode(), state['csrf'].encode()):
                return jsonify({'message': '页面会话已过期，请刷新后重试'}), 403
        if request.path == '/api/security/ws-ticket':
            return None
        # Upload endpoints own their bounded multipart parser. Do not consume
        # request.form / request.values here or their photo stream disappears.
        if request.mimetype == 'multipart/form-data':
            return None
        data = request.get_json(silent=True) or {}
        if request.mimetype == 'application/x-www-form-urlencoded' and request.form.get('data'):
            try:
                data = json.loads(request.form['data'])
            except (ValueError, TypeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        username = data.get('username', data.get('user', request.args.get('username')))
        if request.path == '/api/get-msg' and not username:
            return jsonify({'message': '必须指定用户'}), 400
        if username is None and request.path in DEFAULT_USER_ENDPOINTS:
            username = 'User'
        if username is not None:
            denied = require_chat_user(username)
            if denied:
                return denied

    @app.after_request
    def session_cookie(response):
        if hasattr(g, 'fay_browser_session'):
            response.set_cookie(COOKIE, g.fay_browser_session, httponly=True, samesite='Strict',
                                secure=request.is_secure, path='/', max_age=authority.SESSION_TTL)
        response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') else response.headers.get('Cache-Control', 'no-cache')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    @app.get('/api/security/session')
    def security_session():
        return jsonify({'csrf': g.fay_csrf})

    @app.post('/api/security/ws-ticket')
    def ws_ticket():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({'message': '请求必须为 JSON 对象'}), 400
        try:
            token = authority.issue(g.fay_browser_session, data.get('username'), data.get('audience'), request.host_url.rstrip('/'))
        except ValueError as exc:
            return jsonify({'message': str(exc)}), 403
        return jsonify({'ticket': token})
