import json
from pathlib import Path
import tempfile
import unittest
import archiver as a
import erogamescape as e
import holy_inventory as h
from test_archiver import Response,Fetch

URL="https://web.archive.org/web/20080102030405/http://www.holyseal.net/cgi-bin/mlistview.cgi?prdcode=42"
def payload(url=URL):return {"archived_snapshots":{"closest":{"available":True,"status":"200","url":url}}}

class HolyInventoryTests(unittest.TestCase):
    def test_uses_actual_capture_date_without_year_lock(self):
        self.assertEqual(h.capture_url(payload(),42),URL)
        self.assertIsNone(h.capture_url({"archived_snapshots":{}},42))
        value=payload();value["archived_snapshots"]["closest"]["available"]=False
        self.assertIsNone(h.capture_url(value,42))

    def test_wrong_product_and_external_redirect_are_rejected(self):
        for url in [URL.replace("prdcode=42","prdcode=99"),URL.replace("www.holyseal.net","evil.test"),"https://evil.test/"]:
            with self.assertRaises(ValueError):h.capture_url(payload(url),42)
        with self.assertRaises(ValueError):h.capture_url([],42)

    def test_inventory_fetches_www_capture_and_saves_distribution_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);e.initialize(store)
            store.db.execute("INSERT INTO external_metadata VALUES(?,?,?,?,?)",("erogamescape","holyseal",1,json.dumps({"holyseal_id":42}),a.now()));store.db.commit()
            raw=(Path(__file__).parent/"fixtures/holy-patches.html").read_bytes()
            network=Fetch([Response(b'{"archived_snapshots":{}}'),Response(json.dumps(payload()).encode()),Response(raw,url=URL)])
            self.assertEqual(h.collect(store,network,limit=1),(1,1))
            self.assertIn(42,h.parsed_codes(store))
            self.assertEqual(store.db.execute("SELECT status,chosen_url FROM holy_inventory").fetchone()[0],"parsed")
            self.assertEqual(len(network.requests),3)
            self.assertEqual(h.collect(store,Fetch([])),(0,0))
            store.db.close()
