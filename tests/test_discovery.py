import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.backup import BackupManager
from paper_reader.desktop_api import install_desktop
from paper_reader.discovery import daily_brief
from paper_reader.identity import normalize_doi
from paper_reader.model_client import ModelClient
from paper_reader.settings import Settings
from paper_reader.storage import Database
from paper_reader.sync import _process_records
from test_desktop import sample_pdf


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = replace(Settings.from_env(), root_dir=root, data_dir=root / 'data', db_path=root / 'data/db.sqlite',
                                papers_dir=root / 'data/papers', topics_path=root / 'topics.json')
        self.settings.ensure_dirs()
        self.db = Database(self.settings.db_path)
        self.app = FastAPI(); self.reader = install_desktop(self.app, self.settings, self.db)
        self.client = TestClient(self.app)
        self.headers = {'X-Desktop-Token': self.client.get('/desktop/bootstrap').json()['token']}

    def tearDown(self):
        self.reader.shutdown(); self.client.close(); self.db.close(); self.temp.cleanup()

    def add(self, source, source_id, doi=None, title='A research paper', collected='2026-10-02T23:30:00+00:00'):
        pid, _ = self.db.upsert_paper({'source': source, 'source_id': source_id, 'title': title, 'doi': doi, 'abstract': 'A transformer method.'})
        with self.db._lock:
            self.db._connection.execute('UPDATE papers SET created_at=? WHERE id=?', (collected, pid)); self.db._connection.commit()
        return pid

    def test_conservative_doi_comparison(self):
        expected = '10.1234/a(1).'
        for value in [' DOI:10.1234/A(1). ', 'https://doi.org/10.1234/A%281%29.', 'http://dx.doi.org/10.1234/A(1).', 'urn:doi:10.1234/A(1).']:
            self.assertEqual(normalize_doi(value), expected)
        for value in [None, '', 'not-a-doi', 'https://example.com/10.1234/a', 'https://[bad/10.1234/a', 'https://doi.org/10.1234/a?download=1', '10.1234/', '10.1234/a b', '10.１２３４/a', '10.1234/a\u200b']:
            self.assertIsNone(normalize_doi(value))
        self.assertNotEqual(normalize_doi('10.1234/Ä'), normalize_doi('10.1234/ä'))
        self.assertNotEqual(normalize_doi('10.1234/a.'), normalize_doi('10.1234/a'))

    def test_grouping_keeps_ids_notes_translations_cards_and_positions(self):
        first = self.add('arxiv', 'a', 'doi:10.1234/WORK')
        second = self.add('openalex', 'b', 'https://doi.org/10.1234/work', title='Published version')
        before = {}
        for pid, marker in [(first, '预印本'), (second, '正式版')]:
            path = self.settings.papers_dir / f'{pid}.pdf'; path.write_bytes(sample_pdf())
            self.db.update_local_pdf_path(pid, str(path.relative_to(self.settings.root_dir)))
            self.db.add_annotation(pid, {'page_number': 0, 'annotation_type': 'note', 'text': marker + '笔记'})
            self.db.save_translation(pid, marker, 'zh', marker + '译文', 'test-model')
            self.db.reader_put(pid, 'state', {'last_page': pid, 'tags': [marker], 'folder': marker})
            self.db.reader_put(pid, 'research-card', {'revision': 2, 'thoughts': marker + '想法'})
            before[pid] = [self.db.list_annotations(pid), self.db.reader_get(pid, 'state'), self.db.reader_get(pid, 'research-card')]
        listing = self.client.get('/desktop/papers').json()
        self.assertEqual(listing['total'], 1); self.assertEqual(listing['items'][0]['versions_count'], 2)
        self.assertEqual(self.client.get('/desktop/overview').json()['source_records'], 2)
        variants = self.client.get(f'/desktop/papers/{second}/versions').json()['items']
        self.assertEqual([p['id'] for p in variants], [first, second])
        self.assertTrue(all(p['annotations_count'] == 1 and p['has_research_card'] for p in variants))
        for pid, marker in [(first, '预印本'), (second, '正式版')]:
            self.assertEqual(before[pid], [self.db.list_annotations(pid), self.db.reader_get(pid, 'state'), self.db.reader_get(pid, 'research-card')])
            self.assertEqual(self.db.get_translation(pid, marker, 'zh')['translated_text'], marker + '译文')
            self.assertTrue((self.settings.root_dir / self.db.get_paper(pid)['local_pdf_path']).is_file())

    def test_secondary_version_search_and_filters_remain_reachable(self):
        first = self.add('arxiv', 'a', '10.1234/work')
        second = self.add('openalex', 'b', '10.1234/WORK')
        self.db.add_annotation(second, {'page_number': 4, 'annotation_type': 'note', 'text': '另一版本的独特笔记'})
        self.db.reader_put(second, 'state', {'starred': True, 'folder': '实验参考', 'status': 'done', 'tags': ['独特标签']})
        for query in ['q=独特笔记', 'folder=实验参考', 'status=done', 'starred=true', 'tag=独特标签']:
            result = self.client.get('/desktop/papers?' + query).json()
            self.assertEqual(result['total'], 1); self.assertEqual(result['items'][0]['id'], second)
        self.assertEqual(self.client.get('/desktop/papers').json()['items'][0]['id'], first)

    def test_no_doi_or_different_doi_never_fuzzy_merges(self):
        for n, doi in enumerate([None, None, 'invalid', 'invalid', '10.1234/a', '10.1234/b']):
            self.add('test', str(n), doi, title='Identical titles')
        self.assertEqual(self.client.get('/desktop/papers').json()['total'], 6)

    def test_daily_local_date_first_seen_and_new_versions_no_model(self):
        first = self.add('arxiv', 'a', '10.1234/work')
        self.add('openalex', 'b', '10.1234/work', collected='2026-10-03T01:00:00+00:00')
        self.add('local', 'c', collected='2026-10-03T00:00:00+00:00')
        with patch.object(ModelClient, 'generate') as generate:
            today = self.client.get('/desktop/discovery/brief?date=2026-10-03&utc_offset_minutes=480').json()
            self.assertEqual(today['total'], 2); self.assertEqual(today['new_source_versions'], 1); self.assertEqual(today['local_imports'], 1)
            yesterday = self.client.get('/desktop/discovery/brief?date=2026-10-02&utc_offset_minutes=0').json()
            self.assertEqual(yesterday['total'], 1)
            self.db.upsert_paper({'source': 'arxiv', 'source_id': 'a', 'title': 'Updated metadata', 'doi': '10.1234/work'})
            next_day = self.client.get('/desktop/discovery/brief?date=2026-10-04&utc_offset_minutes=480').json()
            self.assertEqual(next_day['total'], 0)
            self.assertEqual(self.db.get_paper(first)['created_at'], '2026-10-02T23:30:00+00:00')
            generate.assert_not_called()
        self.assertEqual(self.client.get('/desktop/discovery/brief?date=bad').status_code, 422)
        self.assertEqual(self.client.get('/desktop/discovery/brief?utc_offset_minutes=9999').status_code, 422)

    def test_brief_summary_match_reason_pagination_and_complete_export(self):
        topics = {'sources': {'arxiv': {'keywords': ['transformer']}}, 'tags': {}}
        self.settings.topics_path.write_text(json.dumps(topics), encoding='utf-8')
        for n in range(105):
            self.add('test', str(n), title=f'Paper {n:03d}', collected='2026-10-03T01:00:00+00:00')
        self.db.update_ai(1, summary_zh='已有模型摘要，需核对', tags=['机器学习'], score=None)
        first_page = daily_brief(self.db, topics, date(2026, 10, 3), 0, 1, 100)
        self.assertEqual(first_page['total'], 105); self.assertEqual(len(first_page['items']), 100)
        second_page = daily_brief(self.db, topics, date(2026, 10, 3), 0, 2, 100)
        self.assertEqual(len(second_page['items']), 5)
        item = next(p for p in second_page['items'] if p['id'] == 1)
        self.assertTrue(item['brief_is_ai']); self.assertIn('关键词匹配：transformer', item['match_reasons'])
        response = self.client.get('/desktop/discovery/brief.md?date=2026-10-03')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count('\n## Paper '), 105)

    def test_backup_actual_restore_preserves_grouped_versions(self):
        first = self.add('arxiv', 'a', '10.1234/work')
        second = self.add('openalex', 'b', '10.1234/WORK')
        self.db.add_annotation(second, {'page_number': 3, 'annotation_type': 'note', 'text': '保留第二版本笔记'})
        self.db.reader_put(first, 'research-card', {'revision': 1, 'thoughts': '保留第一版本想法'})
        manager = BackupManager(self.settings, self.db); backup = manager.create()
        self.db.reader_put(first, 'research-card', {'revision': 2, 'thoughts': '稍后修改'})
        manager.restore(backup['name'])
        self.assertEqual(self.client.get('/desktop/papers').json()['total'], 1)
        self.assertEqual(len(self.db.work_versions(first)), 2)
        self.assertEqual(self.db.list_annotations(second)[0]['text'], '保留第二版本笔记')
        self.assertEqual(self.db.reader_get(first, 'research-card')['thoughts'], '保留第一版本想法')

    def test_sync_reports_new_work_separately_from_duplicate_source(self):
        enrichment = Mock(summary_zh=None, tags=[], score=None)
        enricher = Mock(); enricher.enrich.return_value = enrichment
        records = [{'source': source, 'source_id': str(n), 'doi': '10.1234/work',
                    'title': 'A paper', 'abstract': 'Source abstract', 'categories': []}
                   for n, source in enumerate(['arxiv', 'openalex'])]
        result = _process_records(self.settings, self.db, source_name='test', records=records, enricher=enricher, download_pdfs=False)
        self.assertEqual(result['inserted'], 2)
        self.assertEqual(result['new_works'], 1); self.assertEqual(result['new_source_versions'], 1)
        again = _process_records(self.settings, self.db, source_name='test', records=records, enricher=enricher, download_pdfs=False)
        self.assertEqual(again['new_works'], 0); self.assertEqual(again['new_source_versions'], 0)


if __name__ == '__main__':
    unittest.main()
