import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.desktop_api import install_desktop
from paper_reader.model_client import ModelClient, ModelResult
from paper_reader.settings import Settings
from paper_reader.storage import Database
from test_desktop import sample_pdf


class AssistantTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data', db_path=root / 'data/db.sqlite',
                                papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.app = FastAPI()
        self.reader = install_desktop(self.app, self.settings, self.db)
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}
        self.pid = self.client.post('/desktop/import', headers=self.headers, files={'file': ('test.pdf', sample_pdf())}).json()['id']
        self.url = f'/desktop/papers/{self.pid}/assistant'

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.temp.cleanup()

    def prepare(self, **values):
        body = {'question': '论文的方法是什么？', **values}
        preview = self.client.post(self.url + '/preview', headers=self.headers, json=body)
        self.assertEqual(preview.status_code, 200, preview.text)
        body.update(preview_id=preview.json()['preview_id'], request_id=str(uuid.uuid4()))
        return body, preview.json()

    def test_blank_question_is_rejected_before_model(self):
        with patch.object(ModelClient, 'generate') as generate:
            response = self.client.post(self.url + '/preview', headers=self.headers, json={'question': ' \n\t '})
            self.assertEqual(response.status_code, 422)
            generate.assert_not_called()

    def answer(self, preview, kind='fact'):
        source = preview['sources'][0]
        return {'kind': kind, 'text': '（模拟回答，仅供逻辑验收）这是一个测试文档。',
                'sources': [{'id': source['id'], 'quote': source['en']}]} 

    def test_preview_does_not_call_model_and_replay_is_idempotent(self):
        with patch.object(ModelClient, 'generate') as model:
            body, preview = self.prepare()
            model.assert_not_called()
            self.assertNotIn('api_key', json.dumps(preview))
            model.return_value = ModelResult(json.dumps({'claims': [self.answer(preview)]}), {}, .1)
            first = self.client.post(self.url, headers=self.headers, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            replay = self.client.post(self.url, headers=self.headers, json=body)
            self.assertTrue(replay.json()['cached'])
            self.assertEqual(model.call_count, 1)
            self.assertEqual(self.client.get(f'/desktop/papers/{self.pid}/discussion').json()['total'], 1)
            cite = first.json()['claims'][0]['sources'][0]
            location = self.client.get(self.url + f"/source/{cite['id']}?source_hash={preview['source_hash']}")
            self.assertEqual(location.status_code, 200)
            self.assertEqual(location.json()['page'], cite['page'])

    def test_invalid_ids_changed_quotes_and_missing_sources_are_not_saved(self):
        body, preview = self.prepare()
        valid = self.answer(preview)
        invalid = [
            {**valid, 'sources': [{'id': 'invented-source', 'quote': valid['sources'][0]['quote']}]},
            {**valid, 'sources': [{'id': valid['sources'][0]['id'], 'quote': 'invented quote 99999'}]},
            {**valid, 'sources': []},
            {**valid, 'sources': [{'id': valid['sources'][0]['id'], 'quote': '        '}]},
        ]
        for claim in invalid:
            with self.subTest(claim=claim), patch.object(ModelClient, 'generate', return_value=ModelResult(json.dumps({'claims': [claim]}), {}, .1)):
                response = self.client.post(self.url, headers=self.headers, json=body)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json()['detail']['code'], 'citation')
        self.assertFalse(self.db.reader_get(self.pid, 'discussion'))

    def test_unknown_is_allowed_but_invalid_json_is_not_saved(self):
        body, preview = self.prepare()
        unknown = {'claims': [{'kind': 'unknown', 'text': '当前上下文未找到，不能推断实验结果。', 'sources': []}]}
        with patch.object(ModelClient, 'generate', return_value=ModelResult(json.dumps(unknown), {}, .1)):
            self.assertEqual(self.client.post(self.url, headers=self.headers, json=body).status_code, 200)
        body, preview = self.prepare()
        with patch.object(ModelClient, 'generate', return_value=ModelResult('not json', {}, .1)):
            self.assertEqual(self.client.post(self.url, headers=self.headers, json=body).status_code, 502)
        self.assertEqual(self.client.get(f'/desktop/papers/{self.pid}/discussion').json()['total'], 1)

    def test_card_prompt_explicitly_requires_fields_container(self):
        body, preview = self.prepare(kind='card')
        fields = {name: [self.answer(preview)] for name in ('problem', 'method', 'result', 'limitations')}
        with patch.object(ModelClient, 'generate', return_value=ModelResult(json.dumps({'fields': fields}), {}, .1)) as model:
            response = self.client.post(self.url, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('顶层 JSON 字段必须是 fields', model.call_args.kwargs['system'])

    def test_flat_card_reply_is_still_rejected_without_saving_generated_fields(self):
        body, preview = self.prepare(kind='card')
        flat = {name: [self.answer(preview)] for name in ('problem', 'method', 'result', 'limitations')}
        with patch.object(ModelClient, 'generate', return_value=ModelResult(json.dumps(flat), {}, .1)):
            response = self.client.post(self.url, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()['detail']['code'], 'format')
        self.assertNotIn('generated', self.db.reader_get(self.pid, 'research-card'))
        self.assertFalse(self.db.reader_get(self.pid, 'discussion'))

    def test_changed_destination_requires_new_preview_before_model_call(self):
        body, preview = self.prepare()
        self.reader.save_config({'base_url': 'http://127.0.0.1:12345'})
        with patch.object(ModelClient, 'generate') as model:
            self.assertEqual(self.client.post(self.url, headers=self.headers, json=body).status_code, 409)
            model.assert_not_called()

    def test_config_switch_during_request_never_switches_approved_client(self):
        body, preview = self.prepare()
        def model_result(client, prompt, **kwargs):
            self.assertEqual(client.base, preview['base_url'])
            self.reader.save_config({'base_url': 'http://127.0.0.1:12345'})
            return ModelResult(json.dumps({'claims': [self.answer(preview)]}), {}, .1)
        with patch.object(ModelClient, 'generate', autospec=True, side_effect=model_result):
            self.assertEqual(self.client.post(self.url, headers=self.headers, json=body).status_code, 409)
        self.assertFalse(self.db.reader_get(self.pid, 'discussion'))

    def test_selected_quote_must_match_source_and_old_pdf_citation_cannot_jump(self):
        doc = self.reader.document(self.pid)
        block = doc['pages'][0]['blocks'][0]
        response = self.client.post(self.url + '/preview', headers=self.headers,
                                    json={'kind': 'explain', 'block_id': block['id'], 'quote': 'not in source'})
        self.assertEqual(response.status_code, 422)
        body, preview = self.prepare(kind='explain', block_id=block['id'], quote=block['en'])
        self.assertIn(block['id'], {s['id'] for s in preview['sources']})
        self.assertEqual(self.client.get(self.url + f"/source/{block['id']}?source_hash={'0' * 64}").status_code, 409)

    def test_research_regeneration_keeps_user_fields_thoughts_and_revisions(self):
        card_url = f'/desktop/papers/{self.pid}/research-card'
        saved = self.client.patch(card_url, headers=self.headers, json={'revision': 0, 'fields': {'method': '我手动整理的方法'}, 'thoughts': '我的实验想法，不交给模型'}).json()
        body, preview = self.prepare(kind='card')
        generated = {'fields': {'problem': [self.answer(preview)], 'method': [], 'result': [], 'limitations': []}}
        with patch.object(ModelClient, 'generate', return_value=ModelResult(json.dumps(generated), {}, .1)) as model:
            response = self.client.post(self.url, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn('我的实验想法', model.call_args.args[0])
        card = self.client.get(card_url).json()
        self.assertEqual(card['thoughts'], '我的实验想法，不交给模型')
        self.assertEqual(card['fields'], {'method': '我手动整理的方法'})
        self.assertEqual(card['revision'], saved['revision'])
        self.assertEqual(card['generated']['fields']['result'][0]['kind'], 'unknown')
        conflict = self.client.patch(card_url, headers=self.headers, json={'revision': 0, 'thoughts': '不能覆盖'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.client.get(card_url).json()['thoughts'], '我的实验想法，不交给模型')
        exported = self.client.get(card_url + '.md').text
        self.assertIn('我的实验想法', exported)
        self.assertIn(preview['sources'][0]['id'], exported)


if __name__ == '__main__':
    unittest.main()
