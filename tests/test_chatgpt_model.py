import io
import json
import unittest

from paper_reader.chatgpt_model import ChatGPTModelClient, read_sse
from paper_reader.chatgpt_auth import RESOURCE
from paper_reader.model_client import ModelError


def stream(*events):
    return io.BytesIO(b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events))


def done(text='translated', status='completed'):
    return {'type': 'response.completed', 'response': {'status': status,
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 5}}}


class Auth:
    active = 'qa-account'
    generation = 1

    def status(self): return {'active': self.active, 'generation': self.generation}
    def access_token(self, account, generation):
        assert account == self.active and generation == self.generation
        return 'qa-token-not-real'


class ChatGPTModelTest(unittest.TestCase):
    def setUp(self):
        self.auth = Auth()
        self.cfg = {'provider': 'chatgpt', 'base_url': RESOURCE, 'model': 'qa-visible-model',
                    'chatgpt_account_id': 'qa-account', 'chatgpt_generation': 1}

    def test_request_uses_public_responses_stream_store_false_and_instructions(self):
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            return stream({'type': 'response.output_text.delta', 'delta': 'translated'}, done())
        result = ChatGPTModelClient(self.cfg, self.auth, opener=open_request).generate('public QA input', system='qa instruction')
        self.assertEqual(requests[0].full_url, RESOURCE + '/responses')
        body = json.loads(requests[0].data)
        self.assertEqual(body, {'model': 'qa-visible-model', 'input': [{'role': 'user', 'content': 'public QA input'}],
            'store': False, 'stream': True, 'instructions': 'qa instruction'})
        self.assertEqual(result.text, 'translated')
        self.assertEqual(result.usage, {'input_tokens': 10, 'output_tokens': 5})

    def test_delta_without_completed_never_becomes_a_success(self):
        for events in [({'type': 'response.output_text.delta', 'delta': 'partial'},),
                       ({'type': 'response.incomplete'},), (done(status='in_progress'),),
                       ({'type': 'response.output_text.delta', 'delta': 'wrong'}, done())]:
            with self.subTest(events=events), self.assertRaises(ModelError):
                read_sse(stream(*events))

    def test_usage_limit_after_stream_stops_without_fallback(self):
        with self.assertRaises(ModelError) as caught:
            read_sse(stream({'type': 'response.output_text.delta', 'delta': 'partial'},
                {'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}}))
        self.assertEqual(caught.exception.code, 'quota')
        self.assertTrue(caught.exception.fatal)

    def test_catalog_preserves_server_order_and_uses_account_slugs(self):
        payload = {'models': [{'slug': 'qa-2', 'display_name': 'QA second', 'visibility': 'list'},
                              {'slug': 'qa-hidden', 'display_name': 'Hidden', 'visibility': 'hidden'},
                              {'slug': 'qa-1', 'display_name': 'QA first', 'visibility': 'list'}]}
        def open_request(request, timeout):
            self.assertEqual(request.full_url, RESOURCE + '/models')
            return io.BytesIO(json.dumps(payload).encode())
        client = ChatGPTModelClient(self.cfg, self.auth, opener=open_request)
        self.assertEqual(client.models(), ['qa-2', 'qa-1'])
        self.assertEqual(client.catalog()[0], {'id': 'qa-2', 'name': 'QA second'})

    def test_account_change_during_response_discards_result(self):
        def open_request(request, timeout):
            self.auth.generation = 2
            return stream(done())
        with self.assertRaises(ModelError) as caught:
            ChatGPTModelClient(self.cfg, self.auth, opener=open_request).generate('QA only')
        self.assertEqual(caught.exception.code, 'auth')

    def test_custom_destination_never_receives_a_token(self):
        with self.assertRaises(ModelError):
            ChatGPTModelClient({**self.cfg, 'base_url': 'https://example.invalid'}, self.auth)


if __name__ == '__main__': unittest.main()
