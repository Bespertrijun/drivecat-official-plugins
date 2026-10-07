import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
import urllib.error
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'plugins/qb-cleanup'))
from qb_cleanup import QbClient, QbError, absolute_path, mapped_path, torrent_files
from devrt import stubs
watch_models = types.ModuleType('app.models.watch')
watch_models.WatchRule = stubs.WatchRule
watch_models.UploadTarget = stubs.UploadTarget
watch_models.UploadTask = stubs.UploadTask
sys.modules['app.models.watch'] = watch_models

# Same SDK injection as devrt.server; no production host imports needed.
sys.modules.setdefault('app', types.ModuleType('app'))
sys.modules.setdefault('app.plugin', types.ModuleType('app.plugin'))
sys.modules['app.plugin.base'] = stubs
spec = importlib.util.spec_from_file_location('qb_plugin_test', ROOT / 'plugins/qb-cleanup/main.py')
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

HASH = 'a' * 40
CONFIG = {'enabled': True, 'base_url': 'http://qb:8080', 'username': 'admin', 'password': 'secret',
          'watcher_ids': [1], 'poll_seconds': 10, 'quiet_seconds': 60, 'timeout_seconds': 5, 'path_mappings': [{'local_path': '/downloads', 'qb_path': '/data'}]}
EVENT = {'source': 'watcher', 'action': 'delete', 'success': True, 'local_path': '/downloads/show/one.mkv'}


class CleanupTests(unittest.TestCase):
    def test_http_login_cookie_lookup_and_delete(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                calls.append((self.path, body, self.headers.get('Cookie')))
                self.send_response(200)
                if self.path.endswith('/login'):
                    self.send_header('Set-Cookie', 'SID=test-session; Path=/; HttpOnly')
                self.end_headers()
                self.wfile.write(b'Ok.' if self.path.endswith('/login') else b'')

            def do_GET(self):
                calls.append((self.path, None, self.headers.get('Cookie')))
                if self.path.endswith('/info'):
                    body = [{'hash': HASH, 'save_path': '/data', 'content_path': '/data/show'}]
                else:
                    body = [{'name': 'show/one.mkv'}, {'name': 'show/two.mkv'}]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = {**CONFIG, 'base_url': f'http://127.0.0.1:{server.server_port}'}
            client = QbClient(config)
            client.login()
            self.assertEqual(client.json('torrents/info')[0]['hash'], HASH)
            client.remove([HASH])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(calls[0][1], {'username': ['admin'], 'password': ['secret']})
        self.assertTrue(all(call[2] == 'SID=test-session' for call in calls[1:]))
        self.assertEqual(calls[-1][0], '/api/v2/torrents/delete')
        self.assertEqual(calls[-1][1], {'hashes': [HASH], 'deleteFiles': ['false']})

    def test_mapping_boundaries_and_longest_prefix(self):
        maps = CONFIG['path_mappings'] + [{'local_path': '/downloads/show', 'qb_path': '/special'}]
        self.assertEqual(mapped_path('/downloads/show/one.mkv', maps), '/special/one.mkv')
        self.assertIsNone(mapped_path('/downloads2/one.mkv', maps))
        self.assertEqual(mapped_path('/downloads/文件 空格.mkv', maps), '/data/文件 空格.mkv')
        for path in ['relative', '/downloads/../x', 'C:\\data', '/data\\x']:
            with self.assertRaises(ValueError):
                absolute_path(path)

    def test_file_paths_and_unsafe_names(self):
        self.assertEqual(torrent_files({'save_path': '/data'}, [{'name': '目录/a.mkv', 'size': 10}]), {'/data/目录/a.mkv': 10})
        for name in ['/data/a.mkv', '../a.mkv', 'x/../a.mkv']:
            with self.assertRaises(ValueError):
                torrent_files({'save_path': '/data'}, [{'name': name, 'size': 10}])

    def test_delete_is_form_post_and_never_deletes_data(self):
        client = QbClient(CONFIG)
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value.read.return_value = b''
        client.remove([HASH])
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.full_url, 'http://qb:8080/api/v2/torrents/delete')
        self.assertEqual(parse_qs(req.data.decode()), {'hashes': [HASH], 'deleteFiles': ['false']})
        self.assertEqual(req.headers['Origin'], 'http://qb:8080')
        client.remove([])
        self.assertEqual(client.opener.open.call_count, 1)
        with self.assertRaises(QbError):
            client.remove(['all'])

    def test_failed_login(self):
        client = QbClient(CONFIG)
        with patch.object(client, 'request', return_value='Fails.'):
            with self.assertRaises(QbError):
                client.login()


    def test_session_expiry_reauthenticates_only_once(self):
        client = QbClient(CONFIG)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'[]'
        client.opener.open = MagicMock(side_effect=[urllib.error.HTTPError('url', 403, 'expired', {}, None), response])
        client.login = MagicMock()
        self.assertEqual(client.json('torrents/info'), [])
        client.login.assert_called_once()
        client.opener.open = MagicMock(side_effect=urllib.error.HTTPError('url', 403, 'banned', {}, None))
        with self.assertRaises(QbError) as error:
            client.json('torrents/info')
        self.assertTrue(error.exception.permanent)
        self.assertEqual(client.opener.open.call_count, 2)

    def test_http_failure_classification(self):
        for code, permanent in [(302, True), (500, False), (503, False), (429, False), (404, True), (401, True)]:
            client = QbClient(CONFIG)
            client.opener.open = MagicMock(side_effect=urllib.error.HTTPError('url', code, 'error', {}, None))
            with self.assertRaises(QbError) as error:
                client.login()
            self.assertEqual(error.exception.permanent, permanent)
        client.opener.open = MagicMock(side_effect=urllib.error.URLError(ssl.SSLCertVerificationError('invalid')))
        with self.assertRaises(QbError) as error:
            client.login()
        self.assertTrue(error.exception.permanent)
        client.opener.open = MagicMock(side_effect=TimeoutError())
        with self.assertRaises(QbError) as error:
            client.login()
        self.assertFalse(error.exception.permanent)


class ConfigApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import asyncio
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = FastAPI()
        self.context = stubs.PluginContext('qb-test', ['fs.read', 'fs.write', 'db.read'], self.app,
                                           self.temp.name, logging.getLogger(__name__), None)
        self.context.hooks = MagicMock()
        self.instance = plugin.QbCleanupPlugin()
        self.context.register_job = MagicMock()
        asyncio.run(self.instance.on_load(self.context))
        self.client = TestClient(self.app)
        self.prefix = '/api/plugins/qb-test/qb-cleanup'

    def test_password_redacted_preserved_and_cleared(self):
        response = self.client.post(self.prefix + '/config', json=CONFIG)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('password', response.json()['config'])
        self.assertTrue(response.json()['config']['password_set'])
        for value in [None, '']:
            self.client.post(self.prefix + '/config', json={**CONFIG, 'password': value})
            stored = plugin.load_config(self.context.get_fs().root)
            self.assertEqual(stored['password'], 'secret' if value is None else '')
        self.assertNotIn('password', self.client.get(self.prefix + '/config').json()['config'])
        self.assertEqual((Path(self.context.get_fs().root) / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_invalid_config_rejected(self):
        for update in [{'base_url': 'file:///etc/passwd'}, {'base_url': 'http://user:pw@host'},
                       {'timeout_seconds': 0}, {'path_mappings': []}, {'watcher_ids': []}, {'quiet_seconds': 0},
                       {'path_mappings': CONFIG['path_mappings'] * 2}]:
            self.assertEqual(self.client.post(self.prefix + '/config', json={**CONFIG, **update}).status_code, 422)

    def test_test_connection_never_deletes_or_saves(self):
        with patch.object(plugin, 'QbClient') as factory:
            factory.return_value.request.return_value = 'v5.0.0'
            response = self.client.post(self.prefix + '/test', json=CONFIG)
            self.assertEqual(response.json(), {'ok': True, 'version': 'v5.0.0'})
            factory.return_value.remove.assert_not_called()
        self.assertFalse((Path(self.context.get_fs().root) / 'config.json').exists())

    def test_existing_scheduler_registration_and_no_hooks(self):
        self.context.hooks.register.assert_not_called()
        self.context.register_job.assert_called_once()
        self.assertEqual(self.context.register_job.call_args.args[0], 'monitor')

    def test_trial_config_migrates_disabled(self):
        directory = self.context.get_fs().root
        old = {k: v for k, v in CONFIG.items() if k != 'watcher_ids'}
        (Path(directory) / 'config.json').write_text(json.dumps(old))
        self.assertFalse(plugin.load_config(directory)['enabled'])

    def test_status_and_history(self):
        self.assertEqual(self.client.get(self.prefix + '/status').status_code, 200)
        self.assertEqual(self.client.post(self.prefix + '/history/clear').status_code, 200)
        self.assertEqual(self.client.post(self.prefix + '/cancel/unknown').status_code, 404)


if __name__ == '__main__':
    unittest.main()
