import json
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import official as o
import official_continue as c
from test_archiver import Fetch,Response
from test_official import GAME,BRAND,PAGE


class OfficialContinuationTests(unittest.TestCase):
    def test_pending_urls_hints_and_seen_survive_reopen_without_refetching_seed(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root);o.initialize(store)
            landing='<a href="trial.html">体験版</a>'
            self.assertEqual(o.scan(store,Fetch([Response(landing.encode(),url=PAGE)]),GAME,BRAND,PAGE,max_pages=1),0)
            row=store.db.execute('SELECT * FROM official_scans').fetchone();marker=json.loads(row['error'])[0]
            self.assertEqual(marker['pending'][0]['hints'],['trial'])
            self.assertEqual(marker['seen'],[PAGE]);store.db.close()
            target='https://key.visualarts.co.jp/anemoi/trial.html'
            fetch=Fetch([Response(b'<a href="download.html">Download</a><a href="trial.zip">Download</a>',url=target)])
            with patch.object(o,'seeds',return_value=iter([(GAME,BRAND,PAGE)])):
                self.assertEqual(c.collect(root,fetcher=fetch),1)
            self.assertEqual([request[0] for request in fetch.requests],[target])
            store=a.Store(root)
            try:
                state=store.db.execute('SELECT status,found,error FROM official_scans').fetchone()
                self.assertEqual(tuple(state),('found',1,None))
                record=json.loads(store.db.execute('SELECT data FROM items').fetchone()[0]);self.assertEqual(record['release_name'],'体験版')
            finally:store.db.close()

    def test_old_bound_marker_is_rebuilt_from_seed(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root);o.initialize(store)
            store.db.execute('INSERT INTO official_scans VALUES(?,?,?,?,?,?)',(GAME['id'],PAGE,'review',0,json.dumps([{'error':c.MARKER,'remaining':24}]),a.now()));store.db.commit();store.db.close()
            fetch=Fetch([Response(b'<h1>Product</h1>',url=PAGE)])
            with patch.object(o,'seeds',return_value=iter([(GAME,BRAND,PAGE)])):self.assertEqual(c.collect(root,fetcher=fetch),1)
            self.assertEqual(fetch.requests[0][0],PAGE)

    def test_prior_failures_are_retained_and_found_counts_accumulate(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root);o.initialize(store)
            try:
                continuation={'seed':PAGE,'pending':[{'url':PAGE,'depth':0,'hints':[]}],'seen':[],'found':7,'failures':[{'url':'https://old.test/','error':'HTTP Error 404'}]}
                o.scan(store,Fetch([Response(b'<section><h2>Patch</h2><a href="patch.zip">Patch</a></section>',url=PAGE)]),GAME,BRAND,PAGE,continuation=continuation)
                row=store.db.execute('SELECT * FROM official_scans').fetchone()
                self.assertEqual(row['found'],8);self.assertEqual(row['status'],'review')
                self.assertEqual(json.loads(row['error']),continuation['failures'])
            finally:store.db.close()

    def test_stale_seed_continuation_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root)
            try:
                with self.assertRaisesRegex(ValueError,'seed changed'):
                    o.scan(store,Fetch([]),GAME,BRAND,PAGE,continuation={'seed':'https://other.test/'})
            finally:store.db.close()

    def test_duplicate_pending_url_keeps_shallowest_observed_depth(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root);o.initialize(store)
            try:
                start='<a href="download_a.html">体験版</a><a href="download_b.html">体験版</a>'
                child='<a href="download_b.html">体験版</a>'
                fetch=Fetch([Response(start.encode(),url=PAGE),Response(child.encode(),url='https://key.visualarts.co.jp/anemoi/download_a.html')])
                o.scan(store,fetch,GAME,BRAND,PAGE,max_pages=2)
                marker=next(error for error in json.loads(store.db.execute('SELECT error FROM official_scans').fetchone()[0]) if error['error']==c.MARKER)
                self.assertEqual(len(marker['pending']),1)
                self.assertEqual(marker['pending'][0]['depth'],1)
            finally:store.db.close()
