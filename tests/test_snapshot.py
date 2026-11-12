import sqlite3
import tempfile
import unittest
from pathlib import Path

import archiver as a
import mirror_sources as m


class SnapshotTests(unittest.TestCase):
    def test_compaction_preserves_links_indexes_and_live_database(self):
        with tempfile.TemporaryDirectory() as folder:
            store=a.Store(folder);m.initialize(store)
            rows=[('kokoron','http://mirror.test/'+str(i)+'/'+'p'*180,
                   'http://files.test/'+str(i)+'/'+'u'*180,'体験版','binary',None,a.now()) for i in range(1000)]
            store.db.executemany('INSERT INTO mirror_links VALUES(?,?,?,?,?,?,?)',rows)
            store.db.execute('CREATE INDEX links_kind ON mirror_links(kind)');store.db.commit()
            path=Path(folder)/'snapshot.sqlite3'
            with sqlite3.connect(path) as backup:
                store.db.backup(backup)
                a._compact_snapshot(backup)
                self.assertEqual(backup.execute('SELECT * FROM mirror_links ORDER BY parent_url').fetchall(),sorted(rows,key=lambda r:r[1]))
                self.assertEqual(backup.execute('PRAGMA quick_check').fetchone()[0],'ok')
                self.assertIsNotNone(backup.execute("SELECT sql FROM sqlite_master WHERE name='links_kind'").fetchone())
                with self.assertRaises(sqlite3.IntegrityError):backup.execute('INSERT INTO mirror_links VALUES(?,?,?,?,?,?,?)',rows[0])
                backup.rollback()
            self.assertNotIn('WITHOUT ROWID',store.db.execute("SELECT sql FROM sqlite_master WHERE name='mirror_links'").fetchone()[0])
            self.assertEqual(store.db.execute('SELECT count(*) FROM mirror_links').fetchone()[0],len(rows))
            store.db.close()

    def test_nullable_legacy_primary_keys_are_preserved(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE mirror_links(site TEXT,parent_url TEXT,url TEXT,label TEXT,kind TEXT,matched_item TEXT,seen_at TEXT,PRIMARY KEY(site,parent_url,url))')
            db.execute("INSERT INTO mirror_links VALUES(NULL,'page','url','label','binary',NULL,NULL)");db.commit()
            a._compact_snapshot(db)
            self.assertEqual(db.execute('SELECT site,url FROM mirror_links').fetchall(),[(None,'url')])

    def test_native_catalogue_without_mirror_links_is_supported(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE example(value TEXT)');db.execute("INSERT INTO example VALUES('preserved')");db.commit()
            a._compact_snapshot(db)
            self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0],'preserved')
