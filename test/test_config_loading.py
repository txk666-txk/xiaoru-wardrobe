from __future__ import annotations

import json
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import config_util


class ConfigLoadingTests(unittest.TestCase):
    def setUp(self):
        self.process_environment = os.environ.copy()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous_cwd)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        # Restore all loader globals so these isolated loads cannot change the
        # credentials/model settings used by other suites in the same process.
        module_state = patch.dict(config_util.__dict__, {'CONFIG_SERVER': dict(config_util.CONFIG_SERVER)})
        module_state.start()
        self.addCleanup(module_state.stop)
        for name, value in {
            '_last_loaded_project_id': None,
            '_last_loaded_center_url': None,
            '_last_loaded_config': None,
            '_last_loaded_from_api': False,
            'system_conf_path': None,
            'config_json_path': None,
        }.items():
            state = patch.object(config_util, name, value)
            state.start()
            self.addCleanup(state.stop)
        quiet = patch.object(config_util.util, 'log')
        quiet.start()
        self.addCleanup(quiet.stop)

    def local_files(self):
        (self.root / 'system.conf').write_text(
            '[key]\ngpt_model_engine=local-test\nfay_url=http://127.0.0.1:5000\n', encoding='utf-8')
        (self.root / 'config.json').write_text('{"memory": {"use_bionic_memory": false}}', encoding='utf-8')

    def center_environment(self, url='https://config.example.test', project='demo-project'):
        os.environ['FAY_CONFIG_CENTER_URL'] = url
        os.environ['FAY_CONFIG_CENTER_ID'] = project
        os.environ['FAY_CONFIG_CENTER_API_KEY'] = 'test-only-key'

    @staticmethod
    def response(model='remote-test'):
        class Response:
            status_code = 200

            def json(self):
                return {'success': True, 'project': {
                    'name': 'Authored test project',
                    'system_config': {'key': {
                        'gpt_model_engine': model,
                        'fay_url': 'http://127.0.0.1:5000',
                    }},
                    'config_json': {'memory': {'use_bionic_memory': False}},
                }}
        return Response()

    def test_util_first_import_has_no_partially_initialized_module_error(self):
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', 'import utils.util; import utils.config_util'],
            cwd=project, env=self.process_environment, capture_output=True,
            text=True, encoding='utf-8', timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_local_files_fail_without_any_config_center_request(self):
        with patch.object(config_util.requests, 'get') as get:
            with self.assertRaisesRegex(FileNotFoundError, 'initialize_local_config'):
                config_util.load_config(force_reload=True)
            get.assert_not_called()
            self.assertIsNone(config_util.load_config_from_api())
            get.assert_not_called()
        self.assertEqual(config_util.CONFIG_SERVER, {'BASE_URL': '', 'PROJECT_ID': '', 'API_KEY': ''})

    def test_local_files_remain_local_and_do_not_request_remote_configuration(self):
        self.local_files()
        with patch.object(config_util.requests, 'get') as get:
            result = config_util.load_config(force_reload=True)
        self.assertEqual(result['gpt_model_engine'], 'local-test')
        self.assertEqual(result['source'], 'local')
        get.assert_not_called()

    def test_partially_configured_center_fails_before_network(self):
        for fields in ({'FAY_CONFIG_CENTER_ID': 'demo'},
                       {'FAY_CONFIG_CENTER_URL': 'https://config.example.test'},
                       {'FAY_CONFIG_CENTER_API_KEY': 'test-key'}):
            with self.subTest(fields=tuple(fields)), patch.dict(os.environ, fields, clear=True):
                with patch.object(config_util.requests, 'get') as get:
                    with self.assertRaisesRegex(ValueError, 'FAY_CONFIG_CENTER_URL'):
                        config_util.load_config(force_reload=True)
                get.assert_not_called()

    def test_explicit_center_parses_configuration_with_bounded_request_and_reuses_memory(self):
        self.center_environment(project='demo/project')
        with patch.object(config_util.requests, 'get', return_value=self.response()) as get:
            result = config_util.load_config(force_reload=True)
            repeated = config_util.load_config()
        get.assert_called_once_with(
            'https://config.example.test/api/projects/demo%2Fproject/config',
            headers={'X-API-Key': 'test-only-key', 'Content-Type': 'application/json'},
            timeout=(3.05, 10), allow_redirects=False,
        )
        self.assertEqual(result['source'], 'api')
        self.assertEqual(result['gpt_model_engine'], 'remote-test')
        self.assertIs(repeated, result)
        self.assertTrue((self.root / 'cache_data' / 'config.json').is_file())
        self.assertFalse((self.root / 'cache_data' / 'system.conf').exists())

    def test_explicit_center_reuses_matching_environment_cache_when_unavailable(self):
        self.center_environment()
        with patch.object(config_util.requests, 'get', return_value=self.response()):
            result = config_util.load_config(force_reload=True)
        config_util._last_loaded_config = None
        config_util._last_loaded_from_api = False
        with patch.object(config_util.requests, 'get', side_effect=ConnectionError('offline')) as get:
            cached = config_util.load_config()
        get.assert_called_once()
        self.assertEqual(cached['source'], 'api')
        self.assertEqual(cached['gpt_model_engine'], result['gpt_model_engine'])

    def test_changing_center_url_cannot_reuse_old_credentials_or_memory(self):
        self.center_environment()
        with patch.object(config_util.requests, 'get', return_value=self.response()):
            config_util.load_config(force_reload=True)
        os.environ['FAY_CONFIG_CENTER_URL'] = 'https://different-config.example.test'
        with patch.object(config_util.requests, 'get', side_effect=ConnectionError('offline')) as get:
            with self.assertRaisesRegex(RuntimeError, '缓存不完整'):
                config_util.load_config()
        get.assert_called_once()

    def test_disabled_center_cannot_implicitly_reuse_api_config(self):
        self.center_environment()
        with patch.object(config_util.requests, 'get', return_value=self.response()):
            config_util.load_config(force_reload=True)
        for name in ('FAY_CONFIG_CENTER_URL', 'FAY_CONFIG_CENTER_ID', 'FAY_CONFIG_CENTER_API_KEY'):
            os.environ.pop(name)
        with patch.object(config_util.requests, 'get') as get:
            with self.assertRaisesRegex(FileNotFoundError, 'initialize_local_config'):
                config_util.load_config()
        get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
