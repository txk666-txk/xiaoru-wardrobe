from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, render_template

from gui.avatar_assets import VENDOR_FILES, avatar_template_context
from gui.shared_assets import SharedStaticFlask


PROJECT = Path(__file__).resolve().parents[1]


class SharedAssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        own, shared = root / 'own', root / 'shared'
        own.mkdir()
        shared.mkdir()
        (shared / 'vue.js').write_text('shared-runtime', encoding='utf-8')
        (shared / 'theme.css').write_text('shared-theme', encoding='utf-8')
        (own / 'theme.css').write_text('workbench-theme', encoding='utf-8')
        (root / 'private.txt').write_text('private', encoding='utf-8')
        self.client = SharedStaticFlask(
            __name__, static_folder=str(own), static_url_path='/static',
            shared_static_folder=shared,
        ).test_client()

    def test_shared_assets_keep_the_existing_urls_and_mime_type(self):
        response = self.client.get('/static/vue.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'shared-runtime')
        self.assertIn('javascript', response.content_type)
        response.close()

    def test_workbench_specific_assets_override_the_shared_copy(self):
        response = self.client.get('/static/theme.css')
        self.assertEqual(response.data, b'workbench-theme')
        response.close()

    def test_missing_and_traversal_paths_stay_inaccessible(self):
        for path in ('unknown.js', '../private.txt', '%2e%2e/private.txt'):
            self.assertEqual(self.client.get('/static/' + path).status_code, 404)


class ReleaseAvatarTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.static = Path(temp.name)
        self.app = Flask(__name__, template_folder=str(PROJECT / 'gui' / 'templates'))

    def install_model(self, model):
        root = self.static / 'live2d'
        for path in (*VENDOR_FILES, f'model/{model}/{model}.model3.json'):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}', encoding='utf-8')

    def render(self):
        with self.app.test_request_context('/'):
            return render_template('index.html', cache_bust='release',
                                   **avatar_template_context(self.static))

    def test_missing_optional_assets_render_a_complete_homepage_without_broken_scripts(self):
        with patch.dict(os.environ, {'FAY_LIVE2D_MODEL': 'xiaoru_1'}):
            html = self.render()
        self.assertIn('小如陪你挑选', html)
        self.assertIn('xiaoru-avatar.svg', html)
        self.assertNotIn('live2d/vendor/', html)
        self.assertIn('js/styling.js', html)

    def test_configured_model_preserves_the_live2d_runtime_and_model_name(self):
        self.install_model('my_authorized_model')
        with patch.dict(os.environ, {'FAY_LIVE2D_MODEL': 'my_authorized_model'}):
            html = self.render()
        self.assertIn('live2d/vendor/l2d.js', html)
        self.assertIn('data-model="my_authorized_model"', html)
        self.assertIn('js/live2d-avatar.js', html)

    def test_invalid_model_cannot_escape_the_local_asset_directory(self):
        with patch.dict(os.environ, {'FAY_LIVE2D_MODEL': '../private'}):
            context = avatar_template_context(self.static)
        self.assertEqual(context['live2d_model'], 'xiaoru_1')
        self.assertFalse(context['live2d_assets_available'])

    def test_local_customer_portrait_is_optional_and_independent_of_live2d(self):
        self.assertEqual(avatar_template_context(self.static)['customer_avatar_asset'],
                         'images/xiaoru-avatar.svg')
        portrait = self.static / 'images' / 'customer-avatar-local.png'
        portrait.parent.mkdir()
        portrait.write_bytes(b'local-portrait')
        context = avatar_template_context(self.static)
        self.assertEqual(context['customer_avatar_asset'], 'images/customer-avatar-local.png')
        self.assertFalse(context['live2d_assets_available'])

    def test_home_settings_and_mcp_share_the_selected_customer_portrait(self):
        portrait = self.static / 'images' / 'customer-avatar-local.png'
        portrait.parent.mkdir()
        for use_local in (False, True):
            if use_local:
                portrait.write_bytes(b'local-portrait')
            expected = '/static/images/' + ('customer-avatar-local.png' if use_local else 'xiaoru-avatar.svg')
            for folder, template in (('gui', 'index.html'), ('gui', 'setting.html'), ('faymcp', 'Page3.html')):
                with self.subTest(local=use_local, template=template):
                    app = Flask(__name__, template_folder=str(PROJECT / folder / 'templates'))
                    with app.test_request_context('/'):
                        html = render_template(template, cache_bust='release', mcp_servers=[],
                                               **avatar_template_context(self.static))
                    self.assertIn('window.FAY_CUSTOMER_AVATAR_URL = "' + expected + '";', html)
                    self.assertIn('src="' + expected + '"', html)
                    if template == 'index.html':
                        self.assertEqual(html.count('src="' + expected + '"'), 3)


if __name__ == '__main__':
    unittest.main()
