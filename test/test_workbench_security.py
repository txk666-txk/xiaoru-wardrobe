from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, jsonify

from core.browser_security import BrowserAuthority
from gui.security import install_security, public_image, safe_user_directory


class WorkbenchSecurityTests(unittest.TestCase):
    def setUp(self):
        self.authority = BrowserAuthority()
        self.patches = [patch('gui.security.authority', self.authority)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.users = {}
        self.app = Flask(__name__)
        install_security(self.app, lambda: self.users)
        self.app.add_url_rule('/api/submit', endpoint='submit', view_func=lambda: jsonify(ok=True), methods=['POST'])
        self.app.add_url_rule('/api/send', endpoint='send', view_func=lambda: jsonify(ok=True), methods=['POST'])
        self.app.add_url_rule('/api/get-msg', endpoint='history', view_func=lambda: jsonify(messages=['private']), methods=['POST'])
        self.client = self.app.test_client()

    def csrf(self, client=None, **kwargs):
        return (client or self.client).get('/api/security/session', **kwargs).json['csrf']

    def test_remote_host_and_dns_rebinding_are_denied(self):
        for kwargs in ({'environ_base': {'REMOTE_ADDR': '192.168.1.20'}}, {'base_url': 'http://evil.test'}):
            self.assertEqual(self.client.get('/api/security/session', **kwargs).status_code, 403)

    def test_all_writes_require_csrf_and_matching_origin(self):
        token = self.csrf()
        self.assertEqual(self.client.post('/api/submit', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/submit', json={}, headers={'X-Fay-CSRF': token, 'Origin': 'https://evil.test'}).status_code, 403)
        good = self.client.post('/api/submit', json={}, headers={'X-Fay-CSRF': token, 'Origin': 'http://localhost'})
        self.assertEqual(good.status_code, 200)
        self.assertNotIn('Access-Control-Allow-Origin', good.headers)
        cookie = good.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)

    def test_credentials_enforced_on_api_and_broken_config_fails_closed(self):
        self.users = {'operator': 'test-password'}
        self.assertEqual(self.client.get('/api/security/session').status_code, 401)
        self.assertEqual(self.client.get('/api/security/session', auth=('operator', 'wrong')).status_code, 401)
        self.assertEqual(self.client.get('/api/security/session', auth=('operator', 'test-password')).status_code, 200)
        self.users = None
        self.assertEqual(self.client.get('/api/security/session').status_code, 503)

    def test_browser_cannot_claim_another_active_chat_or_history(self):
        headers = {'X-Fay-CSRF': self.csrf()}
        self.assertEqual(self.client.post('/api/send', json={'username': 'User'}, headers=headers).status_code, 200)
        other = self.app.test_client()
        other_headers = {'X-Fay-CSRF': self.csrf(other)}
        for endpoint in ('/api/send', '/api/get-msg', '/api/security/ws-ticket'):
            response = other.post(endpoint, json={'username': 'User', 'audience': 'web'}, headers=other_headers)
            self.assertEqual(response.status_code, 403, endpoint)
            self.assertNotIn('private', response.get_data(as_text=True))
        self.assertEqual(other.post('/api/get-msg', json={}, headers=other_headers).status_code, 400)
        self.assertEqual(other.post('/api/send', json={'username': 'Guest'}, headers=other_headers).status_code, 200)

    def test_other_session_csrf_and_missing_ws_username_are_denied(self):
        token = self.csrf()
        other = self.app.test_client()
        self.csrf(other)
        self.assertEqual(other.post('/api/submit', headers={'X-Fay-CSRF': token}).status_code, 403)
        self.assertEqual(self.client.post('/api/security/ws-ticket', json={'audience': 'web'}, headers={'X-Fay-CSRF': token}).status_code, 403)

    def test_safe_directory_rejects_paths_before_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for username in ('../other', '..\\other', 'C:\\Windows', '/tmp', '.', '..', 'CON', 'name.', 'AUX.txt'):
                with self.assertRaises(ValueError, msg=username):
                    safe_user_directory(username, root)
            self.assertEqual(safe_user_directory('顾客-1', root), root / '顾客-1')

    def test_private_photos_and_outside_images_cannot_use_public_image_api(self):
        for name in ('memory/private.jpg', 'agent/data/runtime/tryon/a/result.jpg', 'C:/Windows/photo.png'):
            with self.assertRaises(ValueError):
                public_image(str(Path(name).resolve()))
        allowed = Path('gui/static/images/customer_avatar_beierfasite.png').resolve()
        self.assertEqual(public_image(str(allowed)), str(allowed))


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.auth = BrowserAuthority(clock=lambda: self.now[0])
        self.session, _ = self.auth.session()

    def ticket(self, audience='web'):
        return '/?ticket=' + self.auth.issue(self.session, 'User', audience, 'http://localhost:5000')

    def test_single_use_ticket_binds_origin_audience_and_username(self):
        path = self.ticket()
        binding = self.auth.consume(path, 'web', 'http://localhost:5000')
        self.assertEqual(binding['username'], 'User')
        self.assertIsNone(self.auth.consume(path, 'web', 'http://localhost:5000'))
        self.assertIsNone(self.auth.consume(self.ticket(), 'human', 'http://localhost:5000'))
        self.assertIsNone(self.auth.consume(self.ticket(), 'web', 'http://evil.test'))
        self.assertIsNone(self.auth.consume('/?Username=User', 'web', 'http://localhost:5000'))

    def test_expired_ticket_and_expired_session_are_rejected(self):
        path = self.ticket()
        self.now[0] += 31
        self.assertIsNone(self.auth.consume(path, 'web', 'http://localhost:5000'))
        path = self.ticket()
        self.now[0] += self.auth.SESSION_TTL + 1
        self.assertFalse(self.auth.authorized(self.session, 'User'))
        self.assertIsNone(self.auth.consume(path, 'web', 'http://localhost:5000'))


class WorkbenchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from utils import config_util
            from gui import flask_server
        cls.server = flask_server
        cls.app = getattr(flask_server, '__app')

    def setUp(self):
        self.authority = BrowserAuthority()
        for item in (patch('gui.security.authority', self.authority), patch.object(self.server, 'users', {})):
            item.start()
            self.addCleanup(item.stop)
        self.client = self.app.test_client()
        self.headers = {'X-Fay-CSRF': self.client.get('/api/security/session').json['csrf']}

    def test_legacy_mcp_page_redirects_to_mcp_workbench(self):
        response = self.client.get('/Page3')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, 'http://127.0.0.1:5010/Page3')

    def test_optional_portrait_is_served_and_used_across_both_workbenches(self):
        from faymcp import mcp_service
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp)
            portrait = shared / 'images' / 'customer-avatar-local.png'
            portrait.parent.mkdir()
            portrait.write_bytes(b'local-portrait')
            with patch.object(self.app, '_static_folder', str(shared)), \
                    patch.object(mcp_service.app, 'shared_static_folder', shared):
                clients = ((self.client, '/'), (self.client, '/setting'),
                           (mcp_service.app.test_client(), '/Page3'))
                for client, page in clients:
                    with self.subTest(page=page):
                        response = client.get(page)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('src="/static/images/customer-avatar-local.png"',
                                      response.get_data(as_text=True))
                        image = client.get('/static/images/customer-avatar-local.png')
                        self.assertEqual(image.status_code, 200)
                        self.assertEqual(image.data, b'local-portrait')
                        self.assertEqual(image.mimetype, 'image/png')
                        image.close()

    def test_real_legacy_routes_reject_unauthorized_writes(self):
        for path in ('/api/submit', '/api/delete-user', '/api/clear-memory', '/api/start-live', '/api/stop-live', '/api/open-image', '/transparent-pass'):
            self.assertEqual(self.client.post(path, json={}).status_code, 403, path)

    def test_nested_transparent_message_cannot_impersonate_another_browser(self):
        self.client.post('/api/security/ws-ticket', json={'username': 'Alice', 'audience': 'web'}, headers=self.headers)
        other = self.app.test_client()
        headers = {'X-Fay-CSRF': other.get('/api/security/session').json['csrf']}
        with patch.object(self.server.fay_booter, 'feiFei') as fay:
            for payload in ({'data': {'user': 'Alice', 'text': 'spoofed'}},
                            {'data': json.dumps({'user': 'Alice', 'text': 'spoofed'})}):
                response = other.post('/transparent-pass', json=payload, headers=headers)
                self.assertEqual(response.status_code, 403)
            fay.on_interact.assert_not_called()

    def test_real_delete_rejects_traversal_before_any_database_mutation(self):
        with patch.object(self.server.content_db, 'new_instance') as database:
            for name in ('../outside', '..\\outside', 'C:\\Windows', '.'):
                response = self.client.post('/api/delete-user', json={'username': name}, headers=self.headers)
                self.assertEqual(response.status_code, 400, name)
            database.assert_not_called()

    def test_real_message_lookup_enforces_owner_on_id_lookup(self):
        self.client.post('/api/security/ws-ticket', json={'username': 'Alice', 'audience': 'web'}, headers=self.headers)
        other = self.app.test_client()
        other_headers = {'X-Fay-CSRF': other.get('/api/security/session').json['csrf']}
        with patch.object(self.server.content_db, 'new_instance') as database:
            database.return_value.get_content_by_id.return_value = (1, 'fay', 'text', 'private-secret', 1, 'Alice', 1)
            self.assertEqual(self.client.post('/api/get-msg-by-id', json={'id': 1}, headers=self.headers).json['content'], 'private-secret')
            response = other.post('/api/get-msg-by-id', json={'id': 1}, headers=other_headers)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('private-secret', response.get_data(as_text=True))

    def test_real_local_image_endpoint_rejects_private_photo_directory(self):
        response = self.client.get('/api/local-image', query_string={'path': str(Path('agent/data/runtime/tryon/a/result.jpg').resolve())})
        self.assertEqual(response.status_code, 403)

    def test_real_upload_keeps_multipart_stream_after_security_checks(self):
        from test.test_virtual_tryon import CopyProvider, FakeCommerceService, jpeg_bytes
        from agent.tryon.service import TryOnService
        from gui.commerce_api import _csrf_token, COOKIE_NAME
        with tempfile.TemporaryDirectory() as temp:
            commerce = FakeCommerceService()
            service = TryOnService(provider=CopyProvider(), commerce_service=commerce, runtime_dir=Path(temp))
            try:
                with patch('gui.tryon_api.get_tryon_service', return_value=service), patch('gui.commerce_api.get_commerce_service', return_value=commerce):
                    status = self.client.get('/api/tryon/status')
                    self.assertEqual(status.status_code, 200, status.get_data(as_text=True))
                    raw = self.client.get_cookie(COOKIE_NAME, path='/api').value
                    headers = {**self.headers, 'X-Fay-Demo-CSRF': _csrf_token(raw), 'Origin': 'http://localhost'}
                    response = self.client.post('/api/tryon/jobs', data={
                        'product_id': 'DRESS-1', 'consent': 'true', 'beautify': 'natural',
                        'photo': (io.BytesIO(jpeg_bytes()), 'photo.jpg')
                    }, headers=headers)
                    self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
                    image = self.client.get(response.json['data']['result_url'])
                    self.assertEqual(image.status_code, 200)
                    image.close()
            finally:
                service.close()

    def test_mcp_management_requires_same_protection(self):
        with contextlib.redirect_stdout(io.StringIO()):
            from faymcp import mcp_service
        client = mcp_service.app.test_client()
        with patch.object(mcp_service, 'load_workbench_users', return_value={}):
            token = client.get('/api/security/session').json['csrf']
            self.assertEqual(client.post('/api/mcp/servers', json={}).status_code, 403)
            self.assertEqual(client.get('/api/mcp/servers', environ_base={'REMOTE_ADDR': '192.168.1.5'}).status_code, 403)
            self.assertEqual(client.post('/api/mcp/servers', json={}, headers={'X-Fay-CSRF': token}).status_code, 400)
            script = client.get('/security.js')
            self.assertEqual(script.status_code, 200)
            script.close()
        with patch.object(mcp_service, 'load_workbench_users', return_value={'operator': 'test'}):
            self.assertEqual(client.get('/api/mcp/servers').status_code, 401)


class WebSocketSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Match main.py's import order; avoid the legacy util/config import cycle.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from utils import config_util
            from core import wsa_server
        from websockets.legacy.server import serve
        self.auth = BrowserAuthority()
        self.patcher = patch('core.wsa_server.authority', self.auth)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.server = wsa_server.WebServer(port=0)
        self.listener = await serve(self.server._MyServer__handler, '127.0.0.1', 0)
        self.url = f"ws://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.listener.close()
        await self.listener.wait_closed()

    def ticket(self, username='User'):
        token, _ = self.auth.session()
        return self.auth.issue(token, username, 'web', 'http://localhost:5000')

    async def test_anonymous_socket_is_rejected_before_registration(self):
        from websockets.legacy.client import connect
        async with connect(self.url, origin='http://localhost:5000') as ws:
            await asyncio.wait_for(ws.wait_closed(), 2)
            self.assertEqual(ws.close_code, 1008)
            self.assertFalse(self.server.is_connected('User'))

    async def test_spoofed_username_closes_authenticated_socket(self):
        from websockets.legacy.client import connect
        async with connect(self.url + '/?ticket=' + self.ticket(), origin='http://localhost:5000') as ws:
            self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 2))['panelMsg'], '使用提示：Fay可以独立使用，启动数字人将自动对接。')
            await ws.send(json.dumps({'Username': 'Other'}))
            await asyncio.wait_for(ws.wait_closed(), 2)
            self.assertEqual(ws.close_code, 1008)

    async def test_private_messages_are_delivered_only_to_bound_user(self):
        from websockets.legacy.client import connect
        async with connect(self.url + '/?ticket=' + self.ticket('Alice'), origin='http://localhost:5000') as alice:
            async with connect(self.url + '/?ticket=' + self.ticket('Bob'), origin='http://localhost:5000') as bob:
                await asyncio.sleep(0.05)
                self.server.add_cmd({'Username': 'Alice', 'panelReply': {'content': 'private-alice'}})
                self.server.add_cmd({'panelReply': {'content': 'unscoped-private'}})
                self.server.add_cmd({'Username': 'Bob', 'panelReply': {'content': 'private-bob'}})
                async def private(ws):
                    while True:
                        data = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        if 'panelReply' in data:
                            return data['panelReply']['content']
                self.assertEqual(await private(alice), 'private-alice')
                self.assertEqual(await private(bob), 'private-bob')
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(bob.recv(), 0.1)


if __name__ == '__main__':
    unittest.main()
