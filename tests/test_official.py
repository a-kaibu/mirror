import tempfile
from pathlib import Path
import unittest
import archiver as a
import official as o
from test_archiver import Fetch, Response

GAME={"id":35435,"gamename":"anemoi","brandname":83,"shoukai":"https://key.visualarts.gr.jp/anemoi/"}
BRAND={"id":83,"brandname":"Key"}
PAGE="https://key.visualarts.co.jp/anemoi/download.html"

class OfficialTests(unittest.TestCase):
    def test_actual_anemoi_html(self):
        raw=(Path(__file__).parent/"fixtures/official-anemoi.html").read_bytes()
        records,unknown=o.parse(a.decode_html(raw),PAGE,GAME,BRAND)
        self.assertEqual(len(records),1)
        record=records[0]
        self.assertEqual(record["filename"],"trial-anemoi-ver1.1.zip")
        self.assertEqual(record["release_name"],"体験版 ver1.1")
        self.assertEqual(record["brand"],"Key")
        self.assertEqual(record["fields"]["erogamescape_game_id"],35435)
        self.assertIsNone(record["expected_md5"])
        self.assertIn("azureedge.net",record["mirrors"][0])

    def test_unscoped_download_catalog_requires_product_context(self):
        html='<section><h2>Other title 体験版</h2><a href="other_trial.zip">Download</a></section>'
        records,unknown=o.parse(html,PAGE,GAME,BRAND,scoped=False)
        self.assertEqual(records,[]);self.assertEqual(len(unknown),1)

    def test_checksum_is_not_shared_across_other_files(self):
        html='<main><h2>体験版</h2><p>MD5: '+ 'a'*32 +'</p><a href="first_trial.zip">Download</a><a href="second_trial.zip">Download</a></main>'
        records,_=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(records),2)
        self.assertTrue(all(r["expected_md5"] is None for r in records))

    def test_records_html_and_scan_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);o.initialize(store)
            html=b'<section><h2>anemoi Trial</h2><a href="trial.zip">Download</a></section>'
            fetch=Fetch([Response(html,url=PAGE)])
            self.assertEqual(o.scan(store,fetch,GAME,BRAND,PAGE),1)
            self.assertEqual(store.rows()[0]["site"],"official")
            self.assertEqual(store.db.execute("SELECT status FROM official_scans").fetchone()[0],"found")
            self.assertTrue((store.root/store.db.execute("SELECT path FROM pages").fetchone()[0]).exists())
            store.db.close()

    def test_trial_heading_outside_generic_download_button(self):
        html='<div><div><span>体験版</span></div><div><div><img alt=""></div><div><a href="apt318_tr.zip"><span>ダウンロード</span></a></div></div></div><div><h2>フライヤー</h2><a href="apt318_chirashi.zip">ダウンロード</a></div>'
        records,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual([r["filename"] for r in records],["apt318_tr.zip"])
        self.assertEqual(records[0]["release_name"],"体験版")
        self.assertEqual(unknown,[])

    def test_seed_iterator_does_not_hold_read_snapshot_across_network_work(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);o.initialize(store)
            brand={**BRAND,"url":"https://key.visualarts.gr.jp/"}
            for entity,records in [("brand",[brand]),("game",[GAME,{**GAME,"id":35436}])]:
                for row in records:store.db.execute("INSERT INTO external_metadata VALUES(?,?,?,?,?)",("erogamescape",entity,row["id"],json.dumps(row),a.now()))
            store.db.execute("INSERT INTO metadata_links VALUES(?,?,?,?)",("brand","local",83,"normalized_name"));store.db.commit()
            seeds=o.seeds(store);next(seeds)
            other=a.Store(tmp)
            other.db.execute("INSERT INTO metadata_sync VALUES(?,?,?,?)",("brand",83,1,a.now()));other.db.commit()
            store.db.execute("INSERT INTO official_scans VALUES(?,?,?,?,?,?)",(35435,PAGE,"found",1,None,a.now()));store.db.commit()
            other.db.close();store.db.close()

    def test_old_deeply_nested_html_does_not_exceed_recursion_limit(self):
        html='<div>'*2000+'<section><h2>体験版</h2><a href="trial.zip">Download</a></section>'+'</div>'*2000
        root=a.DOM(html).root
        self.assertEqual(root.text(),"体験版 Download")
        self.assertEqual(len(list(root.all({"a"}))),1)
        records,_=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(records),1)

    def test_trial_container_identifies_image_only_download(self):
        html='<div id="box_special_trial_dlf"><a href="waffle_0171t.zip"><img src="button.png"></a></div>'
        records,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(records),1);self.assertEqual(records[0]["release_name"],"体験版")
        self.assertEqual(unknown,[])

    def test_iframe_news_hint_and_asset_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);o.initialize(store)
            start=b'<a href="download.html">Download</a><a href="download.html">Trial available</a><iframe src="iframe-download_trial.html"></iframe><a href="images/download/photo.jpg">Download</a><a href="download/leaflet.pdf">Download</a>'
            direct=b'<div class="patchbox"><a href="fanzagames_0181t.zip">Download</a></div>'
            frame=b'<a href="waffle_0171t.zip">Download</a>'
            seed='https://key.visualarts.co.jp/anemoi/index.html'
            fetch=Fetch([Response(start,url=seed),Response(direct,url=PAGE),Response(frame,url='https://key.visualarts.co.jp/anemoi/iframe-download_trial.html')])
            self.assertEqual(o.scan(store,fetch,GAME,BRAND,seed),2)
            self.assertEqual(len(fetch.requests),3)
            self.assertTrue(all(__import__('json').loads(row["data"])["release_name"]=="体験版" for row in store.rows()))
            store.db.close()


class ParallelOfficialTests(unittest.TestCase):
    def test_two_publishers_are_scanned_concurrently(self):
        import threading
        from unittest.mock import patch
        barrier=threading.Barrier(2,timeout=5);connections=[]
        other={**GAME,'id':35436,'gamename':'Other'}
        def scan(store,fetcher,game,brand,seed):
            store.db.execute('SELECT count(*) FROM items').fetchone()
            connections.append(id(store.db));barrier.wait();return 0
        with tempfile.TemporaryDirectory() as tmp,patch.object(o,'seeds',return_value=iter([(GAME,BRAND,PAGE),(other,BRAND,'https://other.test/download.html')])),patch.object(o,'scan',side_effect=scan):
            self.assertEqual(o.collect_parallel(tmp,workers=2),2)
            self.assertEqual(len(set(connections)),2)
