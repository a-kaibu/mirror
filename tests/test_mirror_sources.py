import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import mirror_sources as m
from portals import parse_moepedia
import suezou_watch as w
from test_archiver import Fetch,Response,HTML

class MirrorSourcesTests(unittest.TestCase):
    def test_requested_sources_are_registered_without_priority_filtering(self):
        sites=m.definitions()
        self.assertEqual(len(sites),18)
        self.assertTrue({"kokoron","holyseal","multi_inside","tsundere","lapistan","nekoneko","d_station","suezou","getchu","dmm","moepedia","dlsite"}<=set(sites))
        self.assertEqual(m.clean("https://trial.test/file.zip?_gl=tracking&id=12&utm_source=ignore"),"https://trial.test/file.zip?id=12")
        self.assertEqual(m.clean("https://trial.test/file.zip?token=keep"),"https://trial.test/file.zip?token=keep")

    def test_actual_moepedia_trial_block_excludes_purchase_links(self):
        html=a.decode_html((Path(__file__).parent/"fixtures/moepedia-98727.html").read_bytes())
        result=parse_moepedia(html,"https://moepedia.net/game/98727/")
        self.assertEqual({r["filename"] for r in result},{"inheart_0183t.zip","VJ01007035_trial.zip","20261127dessert.rar"})
        self.assertTrue(all(r["title"]=="どいなかガールズゆーとぴあ" and r["brand"]=="DESSERT Soft" for r in result))
        self.assertTrue(all("_gl=" not in r["mirrors"][0] for r in result))
        self.assertEqual(parse_moepedia(html,"https://moepedia.net/trial/"),[])

    def test_getchu_seeds_accept_source_text_ids_and_reject_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);m.initialize(store)
            store.db.execute("CREATE TABLE external_metadata(provider TEXT,entity TEXT,external_id TEXT,data TEXT)")
            for ident,value in enumerate(["1493",1234,"../bad",None,True,"0"]):
                store.db.execute("INSERT INTO external_metadata VALUES(?,?,?,?)",("erogamescape","game",str(ident),json.dumps({"comike":value})))
            store.db.commit();m.seed_platform(store,m.definitions()["getchu"],"live")
            urls={r[0] for r in store.db.execute("SELECT url FROM mirror_frontier")}
            self.assertEqual(urls,{"https://www.getchu.com/soft.phtml?id=1493","https://www.getchu.com/soft.phtml?id=1234"})
            store.db.close()

    def test_capture_host_is_validated(self):
        payload={"archived_snapshots":{"closest":{"available":True,"status":"200","url":"https://web.archive.org/web/20180101000000/http://evil.test/"}}}
        with self.assertRaises(ValueError):m.availability(Fetch([Response(json.dumps(payload).encode())]),"http://suezou.dyndns.org/",m.definitions()["suezou"])

    def test_conflicting_posted_digest_is_not_overridden_by_url_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);m.initialize(store)
            donor=a.parse_detail(HTML,"https://example.test/items/1","lathercraft")[0];store.add(donor)
            changed=HTML.replace(donor["expected_md5"],"f"*32)
            self.assertEqual(m.match_records(store,m.definitions()["kokoron"],changed,"http://kokoron.madoka.org/mirror/item.html",m.known_files(store,"kokoron")),0)
            store.db.close()

    def test_recovery_requires_real_catalogue_not_just_http_200(self):
        self.assertFalse(w.probe(Fetch([Response(b"<title>For sale</title>",url=w.URL)]))["healthy"])
        root='<title>すえぞうのギャルゲー補完計画</title><a href="bishop.html">BISHOP</a>'.encode()
        child=b'<a href="dl2025/bishop/trial.zip">trial</a>'
        self.assertTrue(w.probe(Fetch([Response(root,url=w.URL),Response(child,url=w.URL+"bishop.html")]))["healthy"])

    def test_recovery_starts_collection_and_worker_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            with patch.object(w,"probe",return_value={"healthy":True,"url":w.URL}),patch.object(m,"crawl") as crawl,patch.object(w.subprocess,"run") as run:
                w.check(store,Fetch([]));crawl.assert_called_once();run.assert_called_once()
                self.assertIn("mirror-suezou-archive.service",run.call_args.args[0])
            self.assertEqual(store.db.execute("SELECT healthy FROM source_health").fetchone()[0],1)
            store.db.close()


class MultiInsideTests(unittest.TestCase):
    def test_real_detail_preserves_versions_and_rounded_size(self):
        from pathlib import Path
        from portals import parse_multi_inside
        html=Path("tests/fixtures/multi-inside.html").read_text()
        records=parse_multi_inside(html,"https://web.archive.org/web/20190101085507/http://www.pachi.ac/~multi/download/index/soushinjutsu0.html")
        self.assertEqual(len(records),2)
        self.assertEqual({r["title"] for r in records},{"操心術0"})
        self.assertEqual({r["brand"] for r in records},{"STUDIO邪恋"})
        self.assertEqual(records[1]["expected_md5"],"c1b79bcf1f0d79ec2ec8102f884f8575")
        self.assertEqual(records[1]["release_name"],"体験版")
        self.assertIsNone(records[1]["expected_size"])
        self.assertEqual(records[1]["size_text"],"471MB")
        self.assertEqual(len(records[1]["mirrors"]),6)


class MimimaidTests(unittest.TestCase):
    def test_real_tables_keep_each_product_and_exact_bytes(self):
        from portals import parse_mimimaid
        html=Path("tests/fixtures/mimimaid.html").read_text()
        rows=parse_mimimaid(html,"https://web.archive.org/web/20110609095446/http://mimimaid.moe.hm/html/x18/studio-jyaren/index.htm")
        self.assertEqual(len(rows),10)
        self.assertEqual(rows[0]["title"],"操心術0")
        self.assertEqual(rows[0]["expected_size"],493430161)
        self.assertEqual(rows[0]["expected_md5"],"c1b79bcf1f0d79ec2ec8102f884f8575")
        self.assertEqual(rows[0]["release_name"],"体験版")
        self.assertEqual(rows[-1]["title"],"操心術３")
        self.assertEqual(rows[-1]["expected_size"],112673502)
