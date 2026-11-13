import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import mirror_sources as m
import metadata_watch as w


class MetadataWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name);m.initialize(self.store)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def add(self,site,mode,status='pending'):
        self.store.db.execute('INSERT INTO mirror_frontier(site,url,mode,status) VALUES(?,?,?,?)',(site,'https://source.test/'+site,mode,status));self.store.db.commit()
    def test_pending_sources_exclude_completed_and_native_and_defer_platform_history(self):
        self.add('d_station','archive');self.add('holyseal','archive');self.add('dmm','archive');self.add('dmm','live');self.add('dlsite','live','captured')
        self.assertEqual([s['key'] for s in w.pending_sources(self.tmp.name,'archive')],['d_station'])
        self.assertEqual([s['key'] for s in w.pending_sources(self.tmp.name,'live')],['dmm'])
        self.store.db.execute("UPDATE mirror_frontier SET status='captured' WHERE site='dmm' AND mode='live'");self.store.db.commit()
        self.assertEqual({s['key'] for s in w.pending_sources(self.tmp.name,'archive')},{'d_station','dmm'})
    def test_active_discovery_claim_is_deferred(self):
        self.add('d_station','archive')
        with patch.object(w,'exclusive_file',side_effect=BlockingIOError('busy')):
            self.assertEqual(w.pending_sources(self.tmp.name,'archive'),[])
    def test_batch_uses_existing_shared_crawler_and_page_bound(self):
        self.add('d_station','archive');self.add('getchu','archive')
        with patch.object(m,'parallel_crawl') as crawl:
            self.assertEqual(w.run_batch(self.tmp.name,'archive',100),2)
        args=crawl.call_args.args
        self.assertEqual({s['key'] for s in args[1]},{'d_station','getchu'})
        self.assertEqual(args[2:],('archive',100))
