import base64
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from paper_reader.chatgpt_auth import (ChatGPTAuth, ChatGPTAuthError, ISSUER, TOKEN,
                                     JWKS, DISCOVERY, SCOPES, RESOURCE)


class ChatGPTAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid='qa-key', alg='RS256', use='sig')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.payload = None
        self.revoke_failed = False
        self.auth = ChatGPTAuth(self.root, transport=self.transport)

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, url, form=None, **kwargs):
        self.calls.append((url, form, kwargs))
        if url == JWKS:
            return {'keys': [self.jwk]}
        if url == DISCOVERY:
            return {'issuer': ISSUER, 'revocation_endpoint': ISSUER + '/api/accounts/oauth/revoke'}
        if url.endswith('/oauth/revoke'):
            if self.revoke_failed:
                raise ChatGPTAuthError('controlled offline', 'connection')
            return {}
        self.assertEqual(url, TOKEN)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def tokens(self, attempt, client='oaiapp_qa_first', subject='qa-subject', changes=None, scopes=SCOPES):
        claims = {'iss': ISSUER, 'aud': client, 'sub': subject, 'iat': int(time.time()),
                  'exp': int(time.time()) + 3600, 'nonce': attempt.nonce, 'email': 'qa@example.invalid'}
        claims.update(changes or {})
        return {'id_token': jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'qa-key'}),
                'access_token': 'qa-access-not-a-real-token', 'refresh_token': 'qa-refresh-not-a-real-token',
                'token_type': 'Bearer', 'expires_in': 3600, 'scope': scopes}

    def connect(self, client='oaiapp_qa_first', subject='qa-subject', scopes=SCOPES):
        attempt = self.auth.begin('http://127.0.0.1:55441/auth/callback')
        self.payload = self.tokens(attempt, client, subject, scopes=scopes)
        status = self.auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code', 'client_id': client})
        return status, attempt

    def test_pkce_loopback_host_and_dynamic_client_are_correct_and_persisted(self):
        attempt = self.auth.begin('http://127.0.0.1:55441/auth/callback')
        params = parse_qs(urlsplit(attempt.authorization_url()).query)
        self.assertEqual(params['client_id'], ['dynamic_agent_client'])
        self.assertEqual(params['agent_name_hint'], ['Asteria'])
        self.assertEqual(params['resource'], [RESOURCE])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(params['code_challenge'], [challenge])
        again = ChatGPTAuth(self.root, transport=self.transport).begin('http://127.0.0.1:55442/auth/callback')
        self.assertEqual(attempt.host_id, again.host_id)
        self.assertNotEqual(attempt.state, again.state)
        self.assertNotEqual(attempt.nonce, again.nonce)
        self.assertNotIn(attempt.state, repr(attempt))
        for bad in ['http://localhost:55441/auth/callback', 'https://example.org/auth/callback',
                    'http://127.0.0.1:55441/callback', 'http://127.0.0.1:55441/auth/callback?x=1']:
            with self.subTest(bad=bad), self.assertRaises(ChatGPTAuthError):
                self.auth.begin(bad)

    def test_state_denial_expiry_and_replay_never_exchange_a_code(self):
        for change in ('state', 'deny', 'expired', 'cancelled', 'client_missing'):
            attempt = self.auth.begin('http://127.0.0.1:55441/auth/callback')
            query = {'state': attempt.state, 'code': 'qa-code', 'client_id': 'oaiapp_qa_first'}
            if change == 'state': query['state'] = 'wrong'
            elif change == 'deny': query.update(error='access_denied')
            elif change == 'expired': attempt.expires_at = 0
            elif change == 'cancelled': attempt.cancel()
            else: query.pop('client_id')
            self.calls.clear()
            with self.subTest(change=change), self.assertRaises(ChatGPTAuthError):
                self.auth.complete(attempt, query)
            self.assertEqual(self.calls, [])
        _, attempt = self.connect()
        self.calls.clear()
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code', 'client_id': 'oaiapp_qa_first'})
        self.assertEqual(self.calls, [])

    def test_real_signature_issuer_audience_expiry_nonce_and_subject_validation(self):
        for changes in ({'iss': 'https://evil.invalid'}, {'aud': 'other-client'}, {'exp': 1},
                        {'nonce': 'wrong'}, {'sub': ''}):
            attempt = self.auth.begin('http://127.0.0.1:55441/auth/callback')
            self.payload = self.tokens(attempt, changes=changes)
            with self.subTest(changes=changes), self.assertRaises(ChatGPTAuthError):
                self.auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code', 'client_id': 'oaiapp_qa_first'})
            self.assertEqual(self.auth.status()['accounts'], [])
        attempt = self.auth.begin('http://127.0.0.1:55441/auth/callback')
        self.payload = self.tokens(attempt)
        self.payload['id_token'] = jwt.encode({'sub': 'qa'}, 'qa-test-key-not-real-' * 3, algorithm='HS256', headers={'kid': 'qa-key'})
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code', 'client_id': 'oaiapp_qa_first'})

    def test_forged_signature_cancelled_exchange_and_transient_refresh_preserve_connection(self):
        status, _ = self.connect()
        pending = self.auth.begin('http://127.0.0.1:55442/auth/callback', registration_id=status['active'])
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'iss': ISSUER, 'aud': 'oaiapp_qa_first', 'sub': 'qa-subject',
                  'iat': int(time.time()), 'exp': int(time.time()) + 3600, 'nonce': pending.nonce}
        self.payload = self.tokens(pending)
        self.payload['id_token'] = jwt.encode(claims, wrong_key, algorithm='RS256', headers={'kid': 'qa-key'})
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete(pending, {'state': pending.state, 'code': 'qa-code'})
        self.assertEqual(self.auth.status(), status)
        pending = self.auth.begin('http://127.0.0.1:55442/auth/callback', registration_id=status['active'])
        self.payload = self.tokens(pending)
        token_exchange = self.transport
        def cancel_during_exchange(url, form=None, **kwargs):
            result = token_exchange(url, form, **kwargs)
            if url == TOKEN: pending.cancel()
            return result
        self.auth.transport = cancel_during_exchange
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete(pending, {'state': pending.state, 'code': 'qa-code'})
        self.assertEqual(self.auth.status(), status)
        self.auth.transport = self.transport
        data = self.auth._load(); data['registrations'][status['active']]['expires_at'] = 0
        self.auth._save(data)
        self.payload = ChatGPTAuthError('controlled temporary error', 'connection')
        before = self.auth.credentials.read_bytes()
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token(status['active'], status['generation'])
        self.assertEqual(self.auth.credentials.read_bytes(), before)

    def test_exchange_uses_issued_id_exact_uri_and_pkce_and_no_client_secret(self):
        status, attempt = self.connect()
        form = next(form for url, form, _ in self.calls if url == TOKEN)
        self.assertEqual(form['client_id'], 'oaiapp_qa_first')
        self.assertEqual(form['redirect_uri'], attempt.redirect_uri)
        self.assertEqual(form['code_verifier'], attempt.verifier)
        self.assertEqual(form['resource'], RESOURCE)
        self.assertNotIn('client_secret', form)
        self.assertTrue(status['accounts'][0]['plan_authorized'])
        encoded = self.auth.credentials.read_text(encoding='utf-8')
        self.assertNotIn('qa-access-not-a-real-token', encoded)
        self.assertNotIn('qa-refresh-not-a-real-token', encoded)
        self.assertNotIn('qa@example.invalid', encoded)
        public = json.dumps(status)
        self.assertNotIn('access_token', public)
        self.assertNotIn('id_token', public)
        again = ChatGPTAuth(self.root, transport=self.transport)
        self.assertEqual(again.status(), status)

    def test_identity_only_signin_never_authorizes_inference(self):
        status, _ = self.connect(scopes='openid profile email')
        self.assertTrue(status['accounts'][0]['signed_in'])
        self.assertFalse(status['accounts'][0]['plan_authorized'])
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token(status['active'], status['generation'])

    def test_returning_subject_and_client_cannot_overwrite_existing_connection(self):
        status, _ = self.connect()
        attempt = self.auth.begin('http://127.0.0.1:55442/auth/callback', registration_id=status['active'])
        params = parse_qs(urlsplit(attempt.authorization_url()).query)
        self.assertEqual(params['client_id'], ['oaiapp_qa_first'])
        self.assertNotIn('agent_name_hint', params)
        self.payload = self.tokens(attempt, subject='other-subject')
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete(attempt, {'state': attempt.state, 'code': 'qa-code'})
        self.assertEqual(self.auth.status(), status)

    def test_same_email_separate_registrations_and_old_request_lease_rejected(self):
        first, _ = self.connect()
        second, _ = self.connect('oaiapp_qa_second', 'second-subject')
        self.assertEqual(len(second['accounts']), 2)
        self.assertNotEqual(first['active'], second['active'])
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token(first['active'], first['generation'])
        self.assertEqual(self.auth.access_token(second['active'], second['generation']), 'qa-access-not-a-real-token')

    def test_rotating_refresh_is_atomic_and_permanent_failure_clears_only_tokens(self):
        status, _ = self.connect()
        data = self.auth._load()
        data['registrations'][status['active']]['expires_at'] = 0
        self.auth._save(data)
        self.payload = {'access_token': 'qa-rotated-access', 'refresh_token': 'qa-rotated-refresh',
                        'expires_in': 3600, 'token_type': 'Bearer'}
        self.assertEqual(self.auth.access_token(status['active'], status['generation']), 'qa-rotated-access')
        saved = self.auth._load()['registrations'][status['active']]
        self.assertEqual(saved['refresh_token'], 'qa-rotated-refresh')
        self.assertIn('chatgpt.tokens.use.direct', saved['scopes'])
        data = self.auth._load(); data['registrations'][status['active']]['expires_at'] = 0
        self.auth._save(data)
        self.payload = ChatGPTAuthError('controlled invalid grant', 'invalid_grant', permanent=True)
        with self.assertRaises(ChatGPTAuthError):
            self.auth.access_token(status['active'], status['generation'])
        self.assertFalse(self.auth.status()['accounts'][0]['signed_in'])
        self.assertEqual(self.auth._load()['registrations'][status['active']]['client_id'], 'oaiapp_qa_first')

    def test_logout_and_late_callback_cannot_reactivate_old_session(self):
        status, _ = self.connect()
        pending = self.auth.begin('http://127.0.0.1:55442/auth/callback', registration_id=status['active'])
        self.revoke_failed = True
        result = self.auth.logout()
        self.assertFalse(result['remote_revocation_confirmed'])
        self.assertFalse(self.auth.status()['accounts'][0]['signed_in'])
        self.payload = self.tokens(pending)
        with self.assertRaises(ChatGPTAuthError):
            self.auth.complete(pending, {'state': pending.state, 'code': 'qa-code'})
        self.assertFalse(self.auth.status()['accounts'][0]['signed_in'])


if __name__ == '__main__':
    unittest.main()
