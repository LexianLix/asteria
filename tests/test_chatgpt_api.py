import io
import json
import unittest
from unittest.mock import patch
import zipfile

import test_complex_service as fixtures
import test_chatgpt_auth as auth_fixtures
from paper_reader.chatgpt_auth import SCOPES, ISSUER, TOKEN, JWKS
from paper_reader.chatgpt_model import RESOURCE
from paper_reader.backup import BackupManager
from paper_reader.assistant import ContextRequest
from test_chatgpt_model import stream, done


class ChatGPTApiTest(unittest.TestCase):
    setUp = fixtures.ComplexServiceTest.setUp
    tearDown = fixtures.ComplexServiceTest.tearDown

    @classmethod
    def setUpClass(cls): auth_fixtures.ChatGPTAuthTest.setUpClass()

    def connect(self):
        fixture = auth_fixtures.ChatGPTAuthTest()
        fixture.key, fixture.jwk = auth_fixtures.ChatGPTAuthTest.key, auth_fixtures.ChatGPTAuthTest.jwk
        fixture.calls, fixture.revoke_failed = [], False
        self.reader.chatgpt_auth.transport = fixture.transport
        attempt = self.reader.chatgpt_auth.begin('http://127.0.0.1:55441/auth/callback')
        fixture.payload = fixture.tokens(attempt)
        self.reader.chatgpt_auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code', 'client_id': 'oaiapp_qa_first'})
        return fixture

    def config(self):
        return {**self.reader.config(public=True), 'provider': 'chatgpt', 'base_url': RESOURCE, 'model': 'qa-visible-model'}

    def test_no_browser_or_paper_request_without_explicit_token_guarded_login(self):
        with patch.object(self.reader.chatgpt_login, 'open_browser') as browser:
            self.assertEqual(self.client.post('/desktop/chatgpt/login', json={}).status_code, 403)
            self.assertEqual(self.client.get('/desktop/chatgpt/status').status_code, 200)
            browser.assert_not_called()
        self.assertEqual(self.client.put('/desktop/config', headers=self.headers, json=self.config()).status_code, 422)
        self.assertFalse(self.reader.chatgpt_auth.credentials.exists())

    def test_models_fixed_sentence_stream_and_public_status_never_return_credentials(self):
        self.connect()
        status = self.client.get('/desktop/chatgpt/status').json()
        self.assertNotIn('qa-access-not-a-real-token', json.dumps(status))
        self.assertEqual(self.client.put('/desktop/config', headers=self.headers, json=self.config()).status_code, 200)
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            if request.full_url.endswith('/models'):
                return io.BytesIO(json.dumps({'models': [{'slug': 'qa-visible-model', 'display_name': 'QA only', 'visibility': 'list'}]}).encode())
            return stream(done('测试译文（非真实模型）'))
        with patch('paper_reader.chatgpt_model.open_model_request', side_effect=open_request):
            result = self.client.post('/desktop/model/models', headers=self.headers).json()
            self.assertEqual(result['models'], ['qa-visible-model'])
            self.assertEqual(result['model_catalog'][0]['name'], 'QA only')
            tested = self.client.post('/desktop/model/test', headers=self.headers)
            self.assertEqual(tested.status_code, 200)
        self.assertEqual(requests[-1].full_url, RESOURCE + '/responses')
        payload = json.loads(requests[-1].data)
        self.assertFalse(payload['store']); self.assertTrue(payload['stream'])
        self.assertIn('120 samples', payload['input'][0]['content'])
        self.assertNotIn(self.doc['pages'][0]['blocks'][0]['en'], payload['input'][0]['content'])

    def test_backup_restore_excludes_and_does_not_replace_oauth_credentials_or_host(self):
        self.connect()
        self.reader.save_config(self.config())
        before = self.reader.chatgpt_auth.credentials.read_bytes()
        host = self.reader.chatgpt_auth.host_file.read_bytes()
        manager = BackupManager(self.settings, self.db)
        archive = manager.create()
        with zipfile.ZipFile(manager.directory / archive['name']) as bundle:
            self.assertFalse(any('chatgpt-auth' in name or '.paper-reader-private' in name for name in bundle.namelist()))
            exported = bundle.read('settings.json').decode()
            self.assertNotIn('access_token', exported)
            self.assertNotIn('refresh_token', exported)
            self.assertNotIn('qa-access-not-a-real-token', exported)
        manager.restore(archive['name'])
        self.assertEqual(self.reader.chatgpt_auth.credentials.read_bytes(), before)
        self.assertEqual(self.reader.chatgpt_auth.host_file.read_bytes(), host)

    def test_welcome_once_and_account_change_invalidates_assistant_send_preview(self):
        self.connect(); self.reader.save_config(self.config())
        status = self.reader.chatgpt_auth.status()
        self.assertTrue(status['accounts'][0]['needs_welcome'])
        ack = self.client.post('/desktop/chatgpt/welcome', headers=self.headers, json={'registration_id': status['active']})
        self.assertEqual(ack.status_code, 200)
        self.assertFalse(ack.json()['accounts'][0]['needs_welcome'])
        from paper_reader.assistant import PaperAssistant
        assistant = PaperAssistant(self.reader)
        request = ContextRequest(question='What is the method?')
        first = assistant.prepare(self.pid, request)['preview_id']
        self.reader.chatgpt_auth.select(status['active'])
        self.assertNotEqual(first, assistant.prepare(self.pid, request)['preview_id'])

    def test_login_activation_is_rejected_during_existing_translation(self):
        import threading
        self.connect()
        before = self.reader.chatgpt_auth.status()
        fixture = auth_fixtures.ChatGPTAuthTest()
        fixture.key, fixture.jwk = auth_fixtures.ChatGPTAuthTest.key, auth_fixtures.ChatGPTAuthTest.jwk
        fixture.calls, fixture.revoke_failed = [], False
        self.reader.chatgpt_auth.transport = fixture.transport
        attempt = self.reader.chatgpt_auth.begin('http://127.0.0.1:55442/auth/callback')
        fixture.payload = fixture.tokens(attempt, client='oaiapp_qa_second', subject='second-user')
        self.reader._active[self.pid] = threading.Event()
        try:
            from paper_reader.chatgpt_auth import ChatGPTAuthError
            with self.assertRaises(ChatGPTAuthError):
                self.reader.chatgpt_auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code', 'client_id': 'oaiapp_qa_second'})
        finally:
            self.reader._active.pop(self.pid)
        self.assertEqual(self.reader.chatgpt_auth.status(), before)

    def test_translation_and_selection_reject_missing_or_stale_send_destination(self):
        self.connect(); self.reader.save_config(self.config())
        cfg = self.reader.config()
        binding = {'chatgpt_account_id': cfg['chatgpt_account_id'],
                   'chatgpt_generation': cfg['chatgpt_generation'], 'chatgpt_model': cfg['model']}
        with patch('paper_reader.chatgpt_model.open_model_request') as network:
            response = self.client.post(f'/desktop/papers/{self.pid}/translation', headers=self.headers,
                json={'scope': 'page', 'page_number': 0})
            self.assertEqual(response.status_code, 409)
            self.reader.chatgpt_auth.select(cfg['chatgpt_account_id'])
            response = self.client.post(f'/desktop/papers/{self.pid}/translate-selection', headers=self.headers,
                json={'text': 'QA public text', **binding})
            self.assertEqual(response.status_code, 409)
            network.assert_not_called()
