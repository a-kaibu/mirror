import json
import tempfile
import unittest
import archiver as a
import holy_history as h
from test_archiver import Fetch,Response


class HolyHistoryTests(unittest.TestCase):
    def test_only_observed_product_urls_are_eligible(self):
        self.assertIsNone(h.product_url('http://evil.test/cgi-bin/mlistview.cgi?prdcode=123'))
        self.assertIsNone(h.product_url('http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=123&mode=other'))
        self.assertIsNone(h.product_url('http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=123&prdcode=456'))
        self.assertEqual(h.product_url(a.replay('http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=123','20100101000000')),'http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=123')

    def test_failed_capture_gets_distinct_dates_and_cached_lookup_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root)
            try:
                h.candidates(store,10)
                target='http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=123'
                store.db.execute("INSERT INTO holy_inventory VALUES(?,?,?,?,?,?)",(123,'failed',target,a.replay(target,'20100101000000'),a.now(),'no files'))
                store.db.commit()
                rows=[['timestamp','original','mimetype','statuscode','digest'],['20100101000000',target,'text/html','200','early'],['20110101000000',target,'text/html','200','early'],['20120101000000',target,'text/html','200','later']]
                self.assertEqual(h.collect(store,Fetch([Response(json.dumps(rows).encode())])),(1,2))
                queued=store.db.execute("SELECT url FROM mirror_frontier WHERE site='holyseal'").fetchall()
                self.assertEqual(len(queued),2)
                self.assertTrue(any('20120101000000' in r[0] for r in queued))
                self.assertEqual(h.candidates(store,10),[])
            finally:store.db.close()

    def test_unseen_precedes_retries_and_retries_rotate(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root)
            try:
                h.candidates(store,10)
                urls=['http://holyseal.net/cgi-bin/mlistview.cgi?prdcode='+str(code) for code in [1,2,3]]
                for code,url in enumerate(urls,1):store.db.execute("INSERT INTO holy_inventory VALUES(?,?,?,?,?,?)",(code,'failed',url,None,a.now(),'error'))
                for url,checked in zip(urls,['2026-01-02','2026-01-01']):store.db.execute("INSERT INTO mirror_history VALUES(?,?,?,?,?,?)",('holyseal',url,checked,'failed',None,'timeout'))
                store.db.commit()
                self.assertEqual(h.candidates(store,3),[urls[2],urls[1],urls[0]])
                store.db.execute("UPDATE mirror_history SET checked_at='2026-01-03' WHERE url=?",(urls[1],));store.db.commit()
                self.assertEqual(h.candidates(store,3),[urls[2],urls[0],urls[1]])
            finally:store.db.close()
