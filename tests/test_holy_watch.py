import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import URLError
import archiver as a
import mirror_sources as m
import holy_watch as w
from test_archiver import Fetch,Response,HTML


class HolyWatchTests(unittest.TestCase):
    url='https://web.archive.org/web/20181025223313/http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=1'
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name);m.initialize(self.store)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def add(self,url):
        self.store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES('holyseal',?,'archive')",(url,));self.store.db.commit()
    def test_inventory_on_fresh_store_initializes_external_metadata_without_network(self):
        import holy_inventory as h
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                never=Mock();self.assertEqual(h.collect(store,never,limit=1),(0,0));never.open.assert_not_called()
            finally:store.db.close()

    def test_observed_replay_uses_native_parser_and_saves_html(self):
        self.add(self.url)
        self.assertEqual(w.collect_frontier(self.store,Fetch([Response(HTML.encode(),url=self.url)])),{'captured':1,'failed':0,'review':0})
        self.assertEqual(len(self.store.rows('holyseal')),1)
        row=self.store.db.execute('SELECT * FROM mirror_frontier').fetchone()
        self.assertEqual(row['capture_url'],self.url)
        self.assertTrue((self.store.root/row['path']).exists())
    def test_failed_capture_retains_failure_and_does_not_report_success(self):
        self.add(self.url)
        self.assertEqual(w.collect_frontier(self.store,Fetch([URLError('offline')])),{'captured':0,'failed':1,'review':0})
        self.assertIn('offline',self.store.db.execute('SELECT error FROM mirror_frontier').fetchone()[0])
    def test_unrelated_replay_is_not_fetched_and_batch_bound_is_respected(self):
        self.add(self.url.replace('holyseal.net','foreign.test'));self.add(self.url)
        never=Mock()
        self.assertEqual(w.collect_frontier(self.store,never,limit=1),{'captured':0,'failed':0,'review':1})
        never.open.assert_not_called()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM mirror_frontier WHERE status='pending'").fetchone()[0],1)
