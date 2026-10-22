import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from dataclasses import replace

from paper_reader import __version__
from paper_reader.storage import Database, SCHEMA_VERSION
from paper_reader.backup import BackupManager, check_database
from paper_reader.settings import Settings


class MigrationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.path = self.root / 'data/database.sqlite'
        self.db = Database(self.path)
        self.pid, _ = self.db.upsert_paper({'source': 'local', 'source_id': 'migration-qa', 'title': '升级不能删除的测试记录'})
        self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': '保留笔记'})
        self.db.reader_put(self.pid, 'reading-history', {'sources': {'QA': {'0': {'original': {'text': '保留历史'}}}}})
        self.db.reader_put(self.pid, 'editor', {'edits': {'source-qa': {'zh': '保留手动译文'}}})

    def tearDown(self):
        if self.db is not None: self.db.close()
        self.temp.cleanup()

    def legacy(self):
        self.db._connection.execute('DROP TABLE schema_migrations')
        self.db._connection.execute('PRAGMA user_version = 0')
        self.db._connection.commit(); self.db.close(); self.db = None

    def content(self, path=None):
        connection = sqlite3.connect((path or self.path).as_uri() + '?mode=ro', uri=True)
        try:
            return {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                    for table in ('papers', 'annotations', 'reader_data', 'translations')}
        finally: connection.close()

    def test_new_database_version_and_idempotent_reopen(self):
        self.assertEqual(self.db.schema_version(), SCHEMA_VERSION)
        self.assertIsNone(self.db.migration_snapshot)
        self.assertEqual(self.db._connection.execute('SELECT app_version FROM schema_migrations').fetchone()[0], __version__)
        before = self.content(); self.db.close(); self.db = Database(self.path)
        self.assertEqual(self.content(), before)
        self.assertEqual(self.db._connection.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 1)
        self.assertFalse((self.path.parent / 'migration-snapshots').exists())

    def test_legacy_migration_preserves_content_and_verified_snapshot(self):
        self.legacy(); before = self.content(); self.db = Database(self.path)
        self.assertEqual(self.db.schema_version(), 1); self.assertEqual(self.content(), before)
        snapshot = Path(self.db.migration_snapshot); self.assertTrue(snapshot.is_file())
        self.assertEqual(self.content(snapshot), before)
        connection = sqlite3.connect(snapshot)
        try:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally: connection.close()

    def test_snapshot_reads_committed_wal_data_before_migration(self):
        self.legacy(); writer = sqlite3.connect(self.path)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE annotations SET text='WAL 中刚提交的笔记'"); writer.commit()
            self.assertTrue(Path(str(self.path) + '-wal').exists())
            self.db = Database(self.path)
            self.assertEqual(self.content(Path(self.db.migration_snapshot)), self.content())
            self.assertEqual(self.db.list_annotations(self.pid)[0]['text'], 'WAL 中刚提交的笔记')
        finally: writer.close()

    def test_migration_failure_rolls_back_schema_content_and_version(self):
        self.legacy(); before = self.content()
        real = Database._migrate_v1
        def fail(database, snapshot):
            real(database, snapshot)
            raise RuntimeError('模拟迁移中断')
        with patch.object(Database, '_migrate_v1', fail):
            with self.assertRaisesRegex(RuntimeError, '模拟迁移中断'): Database(self.path)
        self.assertEqual(self.content(), before)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())
        finally: connection.close()
        self.assertEqual(len(list((self.path.parent / 'migration-snapshots').glob('*.sqlite'))), 1)
        self.db = Database(self.path); self.assertEqual(self.content(), before)

    def test_snapshot_failure_prevents_any_migration(self):
        self.legacy(); before = self.content()
        with patch.object(Database, '_migration_backup', side_effect=OSError('模拟磁盘写入失败')), patch.object(Database, '_migrate_v1') as migrate:
            with self.assertRaises(OSError): Database(self.path)
            migrate.assert_not_called()
        self.assertEqual(self.content(), before)

    def test_future_database_refused_before_wal_or_index_writes(self):
        self.db._connection.execute('PRAGMA user_version=99'); self.db._connection.commit(); self.db.close(); self.db = None
        before = self.path.read_bytes(); names = sorted(p.name for p in self.path.parent.iterdir())
        with self.assertRaisesRegex(ValueError, '超出本程序支持范围'): Database(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), names)
        with self.assertRaises(ValueError): check_database(self.path)

    def test_future_version_committed_in_live_wal_is_also_refused(self):
        self.db._connection.execute('PRAGMA user_version=99'); self.db._connection.commit()
        wal = Path(str(self.path) + '-wal')
        self.assertTrue(wal.is_file())
        # Keep the writer open so its committed version has not been checkpointed.
        before = (self.path.read_bytes(), wal.read_bytes())
        with self.assertRaisesRegex(ValueError, '超出本程序支持范围'): Database(self.path)
        self.assertEqual((self.path.read_bytes(), wal.read_bytes()), before)
        self.assertEqual(self.db.list_annotations(self.pid)[0]['text'], '保留笔记')

    def manager(self):
        settings = replace(Settings.from_env(), root_dir=self.root, data_dir=self.path.parent, db_path=self.path,
            papers_dir=self.root / 'data/papers', topics_path=self.root / 'config/topics.json')
        settings.ensure_dirs()
        return BackupManager(settings, self.db)

    def test_legacy_zip_actual_restore_migrates_and_preserves_content(self):
        self.legacy()
        # Create the archive through the real manager with an intentionally open legacy connection.
        holder = Database.__new__(Database); holder.path = self.path
        holder._lock = __import__('threading').RLock(); holder._connection = sqlite3.connect(self.path)
        holder._connection.row_factory = sqlite3.Row
        self.db = holder; manager = self.manager(); before = self.content(); archive = manager.create()
        self.db.close(); self.db = Database(self.path); manager.db = self.db
        self.db.add_annotation(self.pid, {'page_number': 0, 'annotation_type': 'note', 'text': '备份后的笔记'})
        restored = manager.restore(archive['name'])
        self.assertTrue(restored['restored']); self.assertEqual(self.db.schema_version(), 1)
        self.assertEqual(self.content(), before)

    def test_rehashed_future_backup_refused_without_replacing_current_library(self):
        manager = self.manager(); archive = manager.create(); target = manager.resolve(archive['name'])
        with zipfile.ZipFile(target) as original:
            members = {name: original.read(name) for name in original.namelist()}
        staged = self.root / 'future.sqlite'; staged.write_bytes(members['database.sqlite'])
        connection = sqlite3.connect(staged); connection.execute('PRAGMA user_version=99'); connection.commit(); connection.close()
        members['database.sqlite'] = staged.read_bytes()
        manifest = json.loads(members['manifest.json']); entry = manifest['entries']['database.sqlite']
        entry.update(size=len(members['database.sqlite']), sha256=hashlib.sha256(members['database.sqlite']).hexdigest())
        members['manifest.json'] = json.dumps(manifest).encode()
        # Trusted QA test writes only its owned archive to simulate an imported future backup.
        with zipfile.ZipFile(target, 'w') as altered:
            for name, content in members.items(): altered.writestr(name, content)
        before = self.content()
        with self.assertRaisesRegex(ValueError, '超出本程序支持范围'): manager.restore(archive['name'])
        self.assertEqual(self.content(), before)


if __name__ == '__main__': unittest.main()
