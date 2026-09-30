"""Public mode never substitutes the page's CSRF token for owner authentication."""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from public_access import OwnerAccess
from web import create_app


class PublicAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.secret = self.root / 'login.json'
        self.password = 'synthetic-owner-password-' + 'a' * 20
        self.secret.write_text(json.dumps({'username': 'feiran', 'password': self.password}))
        self.env = patch.dict(os.environ, {'WORKBENCH_PUBLIC_HOST': 'origin.internal',
                                          'WORKBENCH_LOGIN_FILE': str(self.secret)})
        self.env.start()
        self.app = self.make_app()
        self.client = TestClient(self.app, base_url='https://origin.internal')
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(('feiran:' + self.password).encode()).decode()}

    def make_app(self):
        return create_app(self.root/'facts.db', self.root/'jobs', token='synthetic-csrf',
                          profile_path=self.root/'profile.json', starter=[])

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.directory.cleanup()

    def test_all_documents_and_methods_require_login_even_with_csrf(self):
        for method, path in [('GET', '/'), ('GET', '/web/app.js'), ('GET', '/api/facts'),
                             ('POST', '/api/cv/upload'), ('GET', '/preview/example/en?token=synthetic-csrf'),
                             ('GET', '/download/example/en?token=synthetic-csrf'), ('GET', '/unknown')]:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers={'X-Workbench-Token': 'synthetic-csrf'})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertIn('Basic', response.headers['www-authenticate'])
                self.assertNotIn('synthetic-csrf', response.text)

    def test_valid_login_still_requires_csrf(self):
        response = self.client.get('/', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn('synthetic-csrf', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get('/api/facts', headers=self.auth).status_code, 403)
        self.assertEqual(self.client.get('/api/facts', headers={**self.auth, 'X-Workbench-Token': 'synthetic-csrf'}).status_code, 200)

    def test_bad_or_malformed_auth_and_forwarded_headers_cannot_bypass(self):
        for value in ['Basic !!!', 'Bearer x', 'Basic ' + 'x'*2000, 'Basic ' + base64.b64encode(b'feiran:wrong').decode()]:
            self.assertEqual(self.client.get('/', headers={'Authorization': value}).status_code, 401)
        self.assertEqual(self.client.get('/', headers={'X-Forwarded-For':'127.0.0.1', 'X-Forwarded-Host':'localhost'}).status_code, 401)

    def test_host_check_and_localhost_auth_remain(self):
        self.assertEqual(self.client.get('/', headers={**self.auth, 'Host':'evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/', headers={'Host':'localhost'}).status_code, 401)
        self.assertEqual(self.client.get('/healthz').status_code, 200)

    def test_startup_fails_closed_on_missing_bad_or_weak_secret(self):
        for content in ['', '{', '{}', '[]', '{"username":"feiran","password":"short"}']:
            self.secret.write_text(content)
            with self.assertRaisesRegex(ValueError, 'owner login secret'):
                self.make_app()
        self.secret.unlink()
        with self.assertRaises(ValueError):
            self.make_app()

    def test_credentials_do_not_appear_in_repr(self):
        self.assertNotIn(self.password, repr(OwnerAccess.from_environment()))

    def test_local_mode_unchanged(self):
        with patch.dict(os.environ, {'WORKBENCH_PUBLIC_HOST': ''}):
            client = TestClient(self.make_app(), base_url='http://127.0.0.1')
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/', headers={'Host':'origin.internal'}).status_code, 403)
            client.close()
