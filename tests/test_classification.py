import json
import threading
import time
import unittest
from unittest.mock import patch

from paper_reader.classification import ClassificationService, MetadataAnswer, consent_path
from paper_reader.model_client import ModelError, ModelResult
import test_desktop as fixtures


class ClassificationTest(unittest.TestCase):
    setUp = fixtures.DesktopTest.setUp
    tearDown = fixtures.DesktopTest.tearDown

    def seed(self, count=2):
        ids = []
        for number in range(count):
            pid, _ = self.db.upsert_paper({'source': 'arxiv', 'source_id': f'classify-{number}',
                'title': f'Public paper {number}', 'abstract': 'Public abstract, not a note.', 'categories': ['cs.AI']})
            ids.append(pid)
        self.db.add_annotation(ids[0], {'page_number': 0, 'annotation_type': 'note', 'text': 'PRIVATE_NOTE_SENTINEL'})
        self.db.reader_put(ids[0], 'research-card', {'thoughts': 'PRIVATE_THOUGHT_SENTINEL'})
        return ids

    def approve(self):
        preview = self.client.post('/desktop/classification/preview', headers=self.headers).json()
        result = self.client.post('/desktop/classification/consent', headers=self.headers,
            json={'enabled': True, 'confirmed': True, 'preview_id': preview['preview_id']})
        self.assertEqual(result.status_code, 200, result.text)
        return preview

    def wait(self):
        deadline = time.monotonic() + 3
        while self.service.classification._active is not None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(self.service.classification._active)
        return self.db.job_get('classification')

    @staticmethod
    def answer(*args, **kwargs):
        return ModelResult(json.dumps({'summary_zh': '仅根据公开摘要生成的测试摘要。',
            'tags': ['人工智能'], 'relevance': .7}), {'input_tokens': 10, 'output_tokens': 5}, .001)

    def test_default_off_preview_is_local_and_requires_token_and_confirmation(self):
        self.seed()
        self.assertFalse(self.client.get('/desktop/classification').json()['enabled'])
        with patch.object(self.service, 'model_client') as model:
            self.client.post('/desktop/classification/preview', headers=self.headers)
            self.assertEqual(self.client.post('/desktop/classification/run', headers=self.headers).status_code, 409)
            self.assertEqual(self.client.post('/desktop/classification/consent', json={'enabled': True}).status_code, 403)
            self.assertEqual(self.client.post('/desktop/classification/consent', headers=self.headers,
                json={'enabled': True}).status_code, 409)
            model.assert_not_called()

    def test_only_public_metadata_sent_completed_results_not_sent_again(self):
        ids = self.seed(); self.approve()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.side_effect = self.answer
            self.service.classification.start()
            self.assertEqual(self.wait()['done'], 2)
            for call in factory.return_value.generate.call_args_list:
                prompt = call.args[0]
                self.assertNotIn('PRIVATE_', prompt)
                metadata = json.loads(prompt.split('\n', 1)[1])
                self.assertEqual(set(metadata), {'title', 'abstract', 'categories'})
            self.service.classification.start(); self.wait()
            self.assertEqual(factory.return_value.generate.call_count, 2)
        self.assertTrue(all(self.db.get_paper(pid)['ai_summary_zh'] for pid in ids))
        self.assertEqual(self.db.reader_get(ids[0], 'research-card')['thoughts'], 'PRIVATE_THOUGHT_SENTINEL')
        self.assertIsNone(self.db.get_paper(self.paper['id'])['ai_summary_zh'])

    def test_model_change_revokes_consent_even_when_changed_back(self):
        self.seed(); self.approve()
        cfg = self.service.config(); old = cfg['model']
        self.service.save_config({'model': 'another-model'})
        self.service.save_config({'model': old})
        self.assertFalse(self.service.classification.status()['enabled'])
        with patch.object(self.service, 'model_client') as factory:
            self.assertEqual(self.service.classification.start()['status'], 'disabled')
            factory.assert_not_called()

    def test_stale_preview_after_model_change_is_rejected(self):
        preview = self.client.post('/desktop/classification/preview', headers=self.headers).json()
        self.service.save_config({'model': 'changed'})
        result = self.client.post('/desktop/classification/consent', headers=self.headers,
            json={'enabled': True, 'confirmed': True, 'preview_id': preview['preview_id']})
        self.assertEqual(result.status_code, 409)

    def test_quota_stops_after_one_request_and_blocks_future_automatic_runs(self):
        ids = self.seed(3); self.approve()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.side_effect = ModelError('用量限制', 'quota', fatal=True)
            self.service.classification.start()
            job = self.wait()
            self.assertEqual((job['status'], job['code'], job['done']), ('paused', 'quota', 0))
            self.assertEqual(self.service.classification.start()['status'], 'disabled')
            self.assertEqual(factory.return_value.generate.call_count, 1)
        self.assertTrue(all(not self.db.get_paper(pid)['ai_summary_zh'] for pid in ids))

    def test_invalid_response_never_saves_or_loops_through_remaining_papers(self):
        ids = self.seed(); self.approve()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.return_value = ModelResult('{"summary_zh":"oops"}', {}, .001)
            self.service.classification.start(); job = self.wait()
            self.assertEqual(job['code'], 'format')
            self.assertEqual(factory.return_value.generate.call_count, 1)
        self.assertTrue(all(not self.db.get_paper(pid)['ai_summary_zh'] for pid in ids))

    def test_disabling_during_request_discards_result_and_prevents_next_send(self):
        ids = self.seed(); self.approve(); entered = threading.Event(); release = threading.Event()
        def delayed(*args, **kwargs):
            entered.set(); release.wait(3); return self.answer()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.side_effect = delayed
            self.service.classification.start()
            self.assertTrue(entered.wait(2))
            self.service.classification.authorize(enabled=False)
            release.set(); self.wait()
            self.assertEqual(factory.return_value.generate.call_count, 1)
        self.assertTrue(all(not self.db.get_paper(pid)['ai_summary_zh'] for pid in ids))

    def test_source_changed_during_request_never_attaches_stale_result(self):
        ids = self.seed(); self.approve()
        def changed(prompt, **kwargs):
            paper = self.db.get_paper(ids[-1]); paper['abstract'] = 'Changed source abstract'
            self.db.upsert_paper(paper)
            return self.answer()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.side_effect = changed
            self.service.classification.start()
            self.assertEqual(self.wait()['code'], 'content')
        self.assertFalse(self.db.get_paper(ids[-1])['ai_summary_zh'])

    def test_restart_never_resumes_interrupted_classification_without_confirmation(self):
        self.approve(); self.db.job_put('classification', {'status': 'running', 'done': 1})
        self.db.jobs_recover()
        resumed = ClassificationService(self.service)
        self.assertFalse(resumed.status()['enabled'])
        self.assertEqual(resumed.start()['status'], 'disabled')
        self.assertEqual(self.db.job_get('classification')['done'], 1)

    def test_private_consent_contains_no_credential_or_account_details(self):
        self.service.save_config({'api_key': 'QA_SECRET_DO_NOT_EXPORT'})
        self.approve()
        text = consent_path(self.settings.root_dir).read_text(encoding='utf-8')
        self.assertNotIn('QA_SECRET', text)
        self.assertNotIn('api_key', text)
        self.assertNotIn('account', text)

    def test_changed_model_while_waiting_never_saves_old_response(self):
        ids = self.seed(); self.approve()
        def changed(*args, **kwargs):
            self.service.save_config({'model': 'changed-during-request'})
            return self.answer()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.side_effect = changed
            self.service.classification.start()
            self.assertEqual(self.wait()['code'], 'consent')
            self.assertEqual(factory.return_value.generate.call_count, 1)
        self.assertTrue(all(not self.db.get_paper(pid)['ai_summary_zh'] for pid in ids))

    def test_metadata_sync_disables_inherited_ollama_and_only_starts_authorized_classifier(self):
        observed = []
        def sync(settings, db, **kwargs):
            observed.append(settings.ollama_url)
            return {'fetched': 0}
        with patch('paper_reader.desktop_api.sync_arxiv', side_effect=sync), patch.object(self.service, 'model_client') as model:
            self.client.post('/desktop/sync', headers=self.headers, json={'source': 'arxiv'})
            deadline = time.monotonic() + 2
            while self.db.job_get('desktop-sync')['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.db.job_get('desktop-sync')['status'], 'done')
            self.assertEqual(observed, [None])
            model.assert_not_called()

    def test_consent_is_excluded_from_actual_backup(self):
        import zipfile
        from paper_reader.backup import BackupManager
        self.approve()
        result = BackupManager(self.settings, self.db).create()
        with zipfile.ZipFile(self.settings.data_dir / 'backups' / result['name']) as archive:
            self.assertFalse(any('classification-consent' in name or '.paper-reader-private' in name for name in archive.namelist()))

    def test_real_client_mock_transport_obeys_chatgpt_completed_stream(self):
        import test_chatgpt_api as api_fixtures
        import test_chatgpt_auth as auth_fixtures
        from test_chatgpt_model import stream, done
        auth_fixtures.ChatGPTAuthTest.setUpClass()
        self.reader = self.service
        fixture = api_fixtures.ChatGPTApiTest.connect(self)
        self.service.save_config(api_fixtures.ChatGPTApiTest.config(self))
        before = self.client.post('/desktop/classification/preview', headers=self.headers).json()
        refused = self.client.post('/desktop/classification/consent', headers=self.headers,
            json={'enabled': True, 'confirmed': True, 'preview_id': before['preview_id']})
        self.assertEqual(refused.status_code, 409)  # The first-time plan notice is separate.
        self.reader.chatgpt_auth.acknowledge_welcome(self.reader.chatgpt_auth.status()['active'])
        ids = self.seed(1); self.approve(); requests = []
        def open_request(request, timeout):
            import io
            requests.append(request)
            if request.full_url.endswith('/models'):
                return io.BytesIO(json.dumps({'models': [{'slug': 'qa-visible-model', 'visibility': 'list'}]}).encode())
            return stream(done(self.answer().text))
        with patch('paper_reader.chatgpt_model.open_model_request', side_effect=open_request):
            self.service.classification.start()
            self.assertEqual(self.wait()['done'], 1)
        payload = json.loads(next(r.data for r in requests if r.full_url.endswith('/responses')))
        self.assertFalse(payload['store']); self.assertTrue(payload['stream'])
        self.assertEqual(payload['model'], 'qa-visible-model')
        self.assertNotIn('PRIVATE_', json.dumps(payload))
        self.assertTrue(self.db.get_paper(ids[0])['ai_summary_zh'])

    def test_batch_cap_preserves_remaining_work_and_completed_cache(self):
        self.seed(31); self.approve()
        with patch.object(self.service, 'model_client') as factory:
            factory.return_value.generate.side_effect = self.answer
            self.service.classification.start()
            self.assertEqual(self.wait()['done'], 30)
            self.assertEqual(factory.return_value.generate.call_count, 30)
            self.assertEqual(self.service.classification.status()['pending'], 1)
            self.service.classification.start()
            self.assertEqual(self.wait()['done'], 1)
            self.assertEqual(factory.return_value.generate.call_count, 31)

    def test_classification_initialization_failure_does_not_change_successful_collection(self):
        with patch('paper_reader.desktop_api.sync_arxiv', return_value={'fetched': 3, 'inserted': 3}), \
             patch.object(self.service.classification, 'start', side_effect=RuntimeError('QA initialization failure')):
            self.client.post('/desktop/sync', headers=self.headers, json={'source': 'arxiv'})
            deadline = time.monotonic() + 2
            while self.db.job_get('desktop-sync')['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(self.db.job_get('desktop-sync')['status'], 'done')
            self.assertEqual(self.db.job_get('desktop-sync')['result']['fetched'], 3)
            self.assertEqual(self.db.job_get('classification')['code'], 'initialization')

    def test_non_chinese_or_unbounded_summary_is_rejected(self):
        from pydantic import ValidationError
        for summary in ('English only', '   ', '中' * 301):
            with self.subTest(summary_length=len(summary)), self.assertRaises(ValidationError):
                MetadataAnswer.model_validate({'summary_zh': summary, 'tags': ['标签'], 'relevance': .5})


if __name__ == '__main__': unittest.main()
