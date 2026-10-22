import unittest
from urllib.parse import urlencode

import httpx

from paper_reader.chatgpt_login import ChatGPTLogin
import test_chatgpt_auth as fixtures


class ChatGPTLoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ChatGPTAuthTest.setUpClass()
        cls.key, cls.jwk = fixtures.ChatGPTAuthTest.key, fixtures.ChatGPTAuthTest.jwk

    def setUp(self):
        self.fixture = fixtures.ChatGPTAuthTest()
        self.fixture.key, self.fixture.jwk = self.key, self.jwk
        self.fixture.setUp()
        self.urls = []
        self.login = ChatGPTLogin(self.fixture.auth, open_browser=lambda url: self.urls.append(url) or True)

    def tearDown(self):
        self.login.shutdown()
        self.fixture.tearDown()

    def test_own_loopback_callback_requires_state_and_never_exposes_secrets(self):
        self.login.start()
        attempt = self.login._attempt
        self.fixture.payload = self.fixture.tokens(attempt)
        with httpx.Client(trust_env=False) as client:
            duplicate = client.get(attempt.redirect_uri + '?' + urlencode([
                ('state', attempt.state), ('state', attempt.state), ('code', 'qa-code')]))
            self.assertEqual(duplicate.status_code, 400)
            self.assertEqual(self.fixture.calls, [])
            self.assertEqual(self.login.status()['status'], 'failed')
            self.assertEqual(self.fixture.auth.status()['accounts'], [])
            # A rejected malformed callback ends that attempt. Start a fresh
            # nonce/state/verifier instead of reviving a failed transaction.
            self.login.start()
            attempt = self.login._attempt
            self.fixture.payload = self.fixture.tokens(attempt)
            response = client.get(attempt.redirect_uri, params={'state': attempt.state,
                'code': 'qa-code', 'client_id': 'oaiapp_qa_first'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('qa-code', response.text)
            self.assertNotIn('qa-access-not-a-real-token', response.text)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(self.login.status()['status'], 'connected')
            replay = client.get(attempt.redirect_uri, params={'state': attempt.state,
                'code': 'qa-code', 'client_id': 'oaiapp_qa_first'})
            self.assertEqual(replay.status_code, 400)
            self.assertEqual(self.login.status()['status'], 'connected')

    def test_cancel_closes_owned_listener_and_preserves_old_connection(self):
        old, _ = self.fixture.connect()
        self.login.start(registration_id=old['active'])
        uri = self.login._attempt.redirect_uri
        self.login.cancel()
        self.assertEqual(self.fixture.auth.status(), old)
        with httpx.Client(trust_env=False, timeout=1) as client, self.assertRaises(httpx.HTTPError):
            client.get(uri)

    def test_denied_authorization_never_exchanges_or_stores_tokens(self):
        self.login.start()
        attempt = self.login._attempt
        with httpx.Client(trust_env=False) as client:
            response = client.get(attempt.redirect_uri, params={'state': attempt.state, 'error': 'access_denied'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(self.fixture.auth.status()['accounts'], [])
        self.assertEqual(self.login.status()['code'], 'denied')


if __name__ == '__main__':
    unittest.main()
