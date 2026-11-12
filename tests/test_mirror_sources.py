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


class NekonekoTests(unittest.TestCase):
    def test_real_page_tracks_product_headings_and_file_scoped_checksums(self):
        from portals import parse_nekoneko
        html=Path("tests/fixtures/nekoneko-pulltop.html").read_text()
        rows=parse_nekoneko(html,"https://web.archive.org/web/20250429112759/http://nekoneko-web.multi-band.net/nekoneko-web18/download/movie/PULLTOP/pulltop_dl-menu.html")
        self.assertEqual(len(rows),12)
        self.assertEqual({r["brand"] for r in rows},{"PULLTOP"})
        trial=next(r for r in rows if r["filename"]=="oozora_trial101.exe")
        self.assertEqual(trial["title"],"この大空に、翼をひろげて")
        self.assertEqual(trial["release_name"],"体験版Ver1.01")
        self.assertEqual(trial["expected_md5"],"d319fe58c3dee8b9e1fe41afd8ae4124")
        self.assertIsNone(trial["expected_size"])
        self.assertEqual(rows[-1]["title"],"神聖にして侵すべからず")


class TsundereTests(unittest.TestCase):
    def test_detail_separates_release_and_exact_size(self):
        from portals import parse_tsundere
        html=Path("tests/fixtures/tsundere-detail.html").read_text()
        rows=parse_tsundere(html,"https://web.archive.org/web/20260613183104/https://mirror.tsundere.ne.jp/mirrorinfo/commerce/adult/studio-jyaren/Soushinjutu0-demomov_mod.html")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["title"],"操心術0")
        self.assertEqual(rows[0]["release_name"],"デモムービー 修正版")
        self.assertEqual(rows[0]["expected_size"],119132956)
        self.assertEqual(rows[0]["expected_md5"],"73bc3af33bf4227f2297fd68127393e9")
        self.assertEqual(len(rows[0]["mirrors"]),2)


class ParallelDiscoveryTests(unittest.TestCase):
    def test_sites_run_concurrently_with_separate_sqlite_connections(self):
        import threading
        barrier=threading.Barrier(2,timeout=5);connections=[]
        def crawl(store,source,fetcher,mode,max_pages):
            connections.append(store.db)
            store.db.execute("SELECT count(*) FROM items").fetchone()
            barrier.wait()
            return 1,0
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,"crawl",side_effect=crawl):
            result=m.parallel_crawl(tmp,[m.definitions()[key] for key in ["getchu","dmm"]],max_pages=1)
            self.assertEqual(result,{"getchu":(1,0),"dmm":(1,0)})
            self.assertEqual(len({id(c) for c in connections}),2)


class PlatformContinuationTests(unittest.TestCase):
    def test_getchu_uses_published_link_and_retains_session(self):
        url="https://www.getchu.com/php/attestation.html?aurl=product"
        gate=b'<a href="https://www.getchu.com/item/123/?gc=gc">'
        gate+='【すすむ】'.encode()+b'</a>'
        network=Fetch([Response(b'<h1>Product</h1>',url="https://www.getchu.com/item/123/?gc=gc")])
        raw,actual,_=m.platform_page(m.definitions()["getchu"],network,gate,url,None)
        self.assertEqual(actual,"https://www.getchu.com/item/123/?gc=gc")
        self.assertIn(b'Product',raw)

    def test_stuck_gate_and_external_continuation_fail(self):
        gate='<a href="https://evil.test/item/123/">【すすむ】</a>'.encode()
        with self.assertRaises(ValueError):m.platform_page(m.definitions()["getchu"],Fetch([]),gate,"https://www.getchu.com/php/attestation.html",None)


class DlsiteTests(unittest.TestCase):
    def test_only_published_trial_link_with_product_identity(self):
        from portals import parse_dlsite
        html='<h1>作品A</h1><table><tr><th>サークル名</th><td>ブランドA</td></tr></table><a href="//trial.dlsite.com/doujin/RJ01714000/RJ01714047_trial.zip">体験版ダウンロード</a><a href="https://www.dlsite.com/purchase/cart">購入</a><a href="https://evil.test/fake.zip">体験版</a>'
        rows=parse_dlsite(html,'https://www.dlsite.com/maniax/work/=/product_id/RJ01714047.html')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['brand'],'ブランドA')
        self.assertEqual(rows[0]['title'],'作品A')
        self.assertEqual(rows[0]['mirrors'],['https://trial.dlsite.com/doujin/RJ01714000/RJ01714047_trial.zip'])
        self.assertEqual(parse_dlsite(html,'https://www.dlsite.com/index.html'),[])


class DStationTests(unittest.TestCase):
    def test_php_downloads_are_not_lost_and_versions_are_separate(self):
        from portals import parse_d_station
        html=Path('tests/fixtures/d-station-2004.html').read_text()
        rows=parse_d_station(html,'https://web.archive.org/web/20040419092336/http://mirror.studio-ramble.com/')
        self.assertEqual(len(rows),5)
        self.assertEqual(rows[0]['title'],'CARNIVAL')
        self.assertEqual(rows[0]['brand'],'S.M.L')
        self.assertEqual(rows[0]['expected_size'],48547013)
        self.assertEqual(rows[0]['expected_md5'],'6928f09c8966cbd9adf4d50205e799b3')
        self.assertIsNone(rows[0]['filename'])
        self.assertEqual(rows[3]['release_name'],'体験版')
        self.assertEqual(rows[1]['title'],rows[2]['title'])
        self.assertNotEqual(rows[1]['id'],rows[2]['id'])


class LapistanTests(unittest.TestCase):
    def test_early_page_assigns_md5_to_each_file(self):
        from portals import parse_lapistan
        html=Path('tests/fixtures/lapistan-2005.html').read_text()
        rows=parse_lapistan(html,'https://web.archive.org/web/20050108213255/http://lapistan.jp/list/PULLTOP/index.htm')
        self.assertEqual(len(rows),4)
        self.assertEqual(rows[1]['title'],'お願いお星さま')
        self.assertEqual(rows[1]['brand'],'PULLTOP')
        self.assertEqual(rows[1]['release_name'],'体験版')
        self.assertEqual(rows[1]['expected_md5'],'b7c11663f8dc6c4fa49c85e06696166c')
        self.assertEqual(rows[1]['published_at'],'2004-04-28')
        self.assertEqual(rows[1]['size_text'],'332.9MB')
        self.assertIsNone(rows[1]['expected_size'])
        self.assertNotEqual(rows[2]['expected_md5'],rows[3]['expected_md5'])


class KokoronTests(unittest.TestCase):
    def test_unclosed_dt_keeps_removed_build_separate(self):
        from portals import parse_kokoron
        rows=parse_kokoron(Path('tests/fixtures/kokoron-2008.html').read_text(),'https://web.archive.org/web/20080505083448/http://kokoron.madoka.org/mirror/studio-jyaren.html')
        self.assertEqual(len(rows),6)
        self.assertEqual(rows[0]['title'],'電波の奴隷')
        self.assertEqual(rows[0]['expected_size'],144329601)
        self.assertEqual(rows[0]['expected_sha256'],'473ff858ea36cc7d1e5c1a91650d846e4f4e3989c4dd21a147634874bc02ab18')
        self.assertEqual(rows[1]['filename'],rows[2]['filename'])
        self.assertNotEqual(rows[1]['expected_md5'],rows[2]['expected_md5'])
        self.assertNotEqual(rows[1]['id'],rows[2]['id'])
        self.assertEqual(rows[2]['mirrors'],[])
        self.assertTrue(rows[2]['fields']['removed_link'])

    def test_published_sha256_is_checked(self):
        from test_archiver import PAYLOAD
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'file.zip';path.write_bytes(PAYLOAD)
            with self.assertRaisesRegex(ValueError,'SHA-256 mismatch'):a.validate(path,{'expected_sha256':'f'*64})
