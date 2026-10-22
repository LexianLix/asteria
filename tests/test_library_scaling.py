"""Small, deterministic regressions for the real large-library call patterns."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import itertools
import random
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from paper_reader.desktop_api import install_desktop
from paper_reader.backup import BackupManager
from paper_reader.settings import Settings
from paper_reader.storage import Database


class LibraryScalingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.path = self.root / 'data/database.sqlite'; self.db = Database(self.path)
        self.ids = []
        for n, doi in enumerate(('https://doi.org/10.1234/SCALING', '10.1234/scaling', None)):
            pid, _ = self.db.upsert_paper({'source': 'test', 'source_id': str(n), 'title': '版本 ' + str(n),
                'doi': doi, 'ai_tags': ['来源标签 ' + str(n)], 'published_at': '2026-10-04'})
            self.ids.append(pid)
        self.db.reader_put(self.ids[0], 'state', {'folder': '主版本', 'tags': ['共享标签'], 'status': 'unread'})
        self.db.reader_put(self.ids[1], 'state', {'folder': '次版本', 'tags': ['共享标签'], 'status': 'reading', 'starred': True})
        self.db.reader_put(self.ids[2], 'state', {'status': 'done'})
        self.db.reader_put(self.ids[0], 'document', self.document(self.ids[0]))

    @staticmethod
    def document(pid):
        return {'pages': [{'number': 0, 'blocks': [{'id': f'qa-{pid}', 'en': '保留原文 evidence', 'box': [.1, .1, .9, .9]}], 'translated': {f'qa-{pid}': '保留机器译文'}}]}

    def tearDown(self):
        if self.db: self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close(); self.db = None; self.db = Database(self.path)

    def test_unchanged_reopen_never_rewrites_document_search_index(self):
        before = self.db.reader_get(self.ids[0], 'search')
        with patch.object(Database, '_index_paper', side_effect=AssertionError('Unchanged content was reindexed')):
            self.reopen()
        self.assertEqual(self.db.reader_get(self.ids[0], 'search'), before)

    def test_legacy_missing_and_malformed_derived_cache_are_rebuilt(self):
        pid = self.ids[0]
        for payload in ({'text': '旧索引'}, {'index_version': 1, 'text': None}):
            self.db.reader_put(pid, 'search', payload); self.reopen()
            self.assertIn('保留原文', self.db.reader_get(pid, 'search')['text'])
            self.assertEqual(self.db.reader_get(pid, 'search')['index_version'], 1)
        self.db._connection.execute("DELETE FROM reader_data WHERE paper_id=? AND kind='search'", (pid,)); self.db._connection.commit()
        self.reopen(); self.assertIn('保留机器译文', self.db.reader_get(pid, 'search')['text'])
        self.db._connection.execute("UPDATE reader_data SET payload='not valid JSON' WHERE paper_id=? AND kind='search'", (pid,)); self.db._connection.commit()
        self.reopen(); self.assertIn('保留原文', self.db.reader_get(pid, 'search')['text'])

    def test_document_and_manual_edit_update_search_in_the_same_transaction(self):
        pid = self.ids[0]; document = self.document(pid); document['pages'][0]['blocks'][0]['en'] = '新版本正文'
        self.db.reader_put_many(pid, {'document': document, 'editor': {'edits': {f'qa-{pid}': {'zh': '独立人工译文'}}}})
        search = self.db.reader_get(pid, 'search')
        self.assertNotIn('保留原文', search['text']); self.assertIn('新版本正文', search['text']); self.assertIn('独立人工译文', search['text'])
        self.reopen(); self.assertEqual(self.db.reader_get(pid, 'search'), search)

    def test_explicit_rebuild_failure_rolls_back_every_derived_cache(self):
        self.db.reader_put(self.ids[1], 'document', self.document(self.ids[1]))
        for pid in self.ids[:2]: self.db.reader_put(pid, 'search', {'text': '旧索引 ' + str(pid)})
        before = [self.db.reader_get(pid, 'search') for pid in self.ids[:2]]
        real = self.db._index_paper
        def fail_second(pid):
            if pid == self.ids[1]: raise RuntimeError('模拟第二篇重建失败')
            real(pid)
        with patch.object(self.db, '_index_paper', side_effect=fail_second):
            with self.assertRaises(RuntimeError): self.db.rebuild_search()
        self.assertEqual([self.db.reader_get(pid, 'search') for pid in self.ids[:2]], before)

    def service_client(self):
        settings = replace(Settings.from_env(), root_dir=self.root, data_dir=self.path.parent, db_path=self.path,
            papers_dir=self.root / 'data/papers', topics_path=self.root / 'topics.json')
        settings.ensure_dirs(); app = FastAPI(); service = install_desktop(app, settings, self.db)
        return service, TestClient(app)

    def test_overview_uses_one_metadata_snapshot_not_per_paper_queries(self):
        service, client = self.service_client(); statements = []
        try:
            self.db._connection.set_trace_callback(statements.append)
            result = client.get('/desktop/overview').json()
            selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
            self.assertLessEqual(len(selects), 4, 'Overview queries scale with paper count')
            self.assertEqual(result['total'], 2); self.assertEqual(result['source_records'], 3)
            self.assertEqual(result['starred'], 1); self.assertEqual(result['folders'], ['主版本', '次版本'])
            self.assertEqual(result['statuses'], {'unread': 1, 'reading': 0, 'done': 1})
            self.assertEqual(result['tags']['共享标签'], 1)
            self.assertTrue(all(result['tags']['来源标签 ' + str(n)] == 1 for n in range(3)))
        finally:
            self.db._connection.set_trace_callback(None); client.close(); service.shutdown()

    def test_grouped_filters_keep_matching_secondary_version_and_pagination(self):
        self.db.reader_put(self.ids[1], 'document', {'pages': [{'number': 0, 'blocks': [{'id': 'secondary', 'en': '次版本独有证据'}], 'translated': {}}]})
        rows, total = self.db.list_papers(query='次版本独有证据', group_doi=True)
        self.assertEqual(total, 1); self.assertEqual(rows[0]['id'], self.ids[1])
        rows, total = self.db.list_papers(folder='次版本', starred=True, group_doi=True)
        self.assertEqual(total, 1); self.assertEqual(rows[0]['id'], self.ids[1])
        rows, total = self.db.list_papers(offset=1, limit=1, group_doi=True)
        self.assertEqual(total, 2); self.assertEqual(rows[0]['id'], self.ids[2])

    def test_backup_restore_rebuilds_legacy_and_current_derived_cache(self):
        pid = self.ids[0]
        settings = replace(Settings.from_env(), root_dir=self.root, data_dir=self.path.parent, db_path=self.path,
            papers_dir=self.root / 'data/papers', topics_path=self.root / 'topics.json')
        settings.ensure_dirs(); manager = BackupManager(settings, self.db)
        for payload in ({'text': '旧版不完整缓存'}, {'index_version': 1, 'text': '版本正确但正文不对应的缓存'}):
            self.db.reader_put(pid, 'search', payload)
            archive = manager.create()
            self.db.reader_put(pid, 'document', {'pages': []})
            manager.restore(archive['name'])
            self.assertEqual(self.db.reader_get(pid, 'search')['index_version'], 1)
            rows, total = self.db.list_papers(query='保留原文', group_doi=True)
            self.assertEqual(total, 1); self.assertEqual(rows[0]['id'], pid)

    def test_grouped_queries_match_independent_filter_oracle(self):
        rng = random.Random(104); fixture = []
        for n in range(40):
            doi = '10.5678/property-' + str(n // 3)
            pid, _ = self.db.upsert_paper({'source': 'oracle', 'source_id': str(n), 'doi': doi,
                'title': 'Oracle ' + str(n), 'abstract': 'Alpha metadata', 'ai_tags': ['来源标签'] if n % 2 else [],
                'published_at': f'2026-10-{n % 3 + 1:02d}'})
            state = {'folder': rng.choice(['文件夹A', '文件夹B']), 'tags': ['个人标签'] if n % 5 else [],
                'status': rng.choice(['unread', 'reading', 'done']), 'starred': bool(n % 4)}
            body = f'body_ {n % 7}' + (' 95%' if n % 6 == 0 else '')
            note = f'note_ {n}' if n % 3 == 0 else ''
            self.db.reader_put_many(pid, {'state': state, 'document': {'pages': [{'number': 0,
                'blocks': [{'id': 'oracle-block', 'en': body}], 'translated': {}}]}})
            if note: self.db.add_annotation(pid, {'page_number': 0, 'annotation_type': 'note', 'text': note})
            paper = self.db.get_paper(pid); paper.update(state=state, body=body, note=note); fixture.append(paper)
        # Other setup rows are included in the oracle rather than hidden by an extra filter.
        for pid in self.ids:
            paper = self.db.get_paper(pid); paper.update(state=self.db.reader_get(pid, 'state'),
                body=self.db.reader_get(pid, 'search').get('text', ''), note=''); fixture.append(paper)
        for query, tag, status, folder, starred in itertools.product([None, 'body_', 'note_', '%', '不存在'],
                [None, '来源标签', '个人标签'], [None, 'unread', 'done'], [None, '文件夹A'], [False, True]):
            matches = []
            for paper in fixture:
                meta = paper['state']
                if query and query.casefold() not in '\n'.join([paper['title'], paper['abstract'], paper['body'], paper['note']]).casefold(): continue
                if tag and tag not in [*paper['ai_tags'], *meta.get('tags', [])]: continue
                if status and meta.get('status', 'unread') != status: continue
                if folder and meta.get('folder') != folder: continue
                if starred and not meta.get('starred'): continue
                matches.append(paper)
            groups = {}
            for paper in sorted(matches, key=lambda p: p['id']):
                key = paper['doi'].lower().removeprefix('https://doi.org/') if paper['doi'] else 'id:' + str(paper['id'])
                groups.setdefault(key, paper)
            expected = sorted(groups.values(), key=lambda p: (p['published_at'] or p['updated_local_at'], -p['id']), reverse=True)
            rows, total = self.db.list_papers(query=query, tag=tag, status=status, folder=folder, starred=starred,
                group_doi=True, offset=1, limit=7)
            self.assertEqual(total, len(expected), (query, tag, status, folder, starred))
            self.assertEqual([p['id'] for p in rows], [p['id'] for p in expected[1:8]])


if __name__ == '__main__': unittest.main()
