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
        self.assertEqual(len(sites),21)
        self.assertIn("alice_archives",sites)
        self.assertIn("alicefree",sites)
        self.assertTrue({"kokoron","holyseal","multi_inside","tsundere","lapistan","nekoneko","d_station","suezou","getchu","dmm","moepedia","dlsite"}<=set(sites))
        self.assertEqual(m.clean("https://trial.test/file.zip?_gl=tracking&id=12&utm_source=ignore"),"https://trial.test/file.zip?id=12")
        self.assertEqual(m.clean("https://trial.test/file.zip?token=keep"),"https://trial.test/file.zip?token=keep")


    def test_legacy_catalogue_links_are_followed_from_archived_homepage(self):
        cases=[('multi_inside','http://web.archive.org/web/20260412082127/http://www.pachi.ac/~multi/',
                '<a href="demo.mirror.shtml">でもみらー</a><a href="download/index/old-list.shtm">旧一覧</a>',
                ['demo.mirror.shtml','download/index/old-list.shtm']),
               ('getchu','http://web.archive.org/web/20220102030405/https://www.getchu.com/',
                '<a href="soft.phtml?id=123">体験版のある作品</a>',['soft.phtml?id=123'])]
        for key,home,html,links in cases:
            with self.subTest(site=key),tempfile.TemporaryDirectory() as folder:
                source={**m.definitions()[key],'seeds':[a.original(home)],'homes':[]}
                store=a.Store(folder)
                try:
                    m.initialize(store)
                    store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES(?,?,'archive')",(key,home));store.db.commit()
                    m.crawl(store,source,Fetch([Response(html.encode(),url=home)]),max_pages=1)
                    pending={r[0] for r in store.db.execute("SELECT url FROM mirror_frontier WHERE status='pending'")}
                    self.assertTrue({a.resolve(home,link) for link in links}<=pending)
                finally:store.db.close()

    def test_retired_getchu_service_uses_dated_archive_lookup(self):
        from urllib.parse import parse_qs,urlsplit
        source=m.definitions()["getchu"]
        self.assertEqual(source["adapter"],"mirror")
        target="https://www.getchu.com/soft.phtml?id=123"
        capture="https://web.archive.org/web/20190102030405/https://www.getchu.com/soft.phtml?id=123"
        payload={"archived_snapshots":{"closest":{"status":"200","available":True,"url":capture}}}
        fetch=Fetch([Response(json.dumps(payload).encode())])
        self.assertEqual(m.availability(fetch,target,source),capture)
        self.assertEqual(parse_qs(urlsplit(fetch.requests[0][0]).query)["timestamp"],["20220101000000"])

    def test_compromised_getchu_names_require_independent_checksum(self):
        url="https://trial.getchu.com/haison_trial.zip"
        html='<h1>Game</h1><a href="'+url+'">体験版</a>'
        donor={"id":"donor","site":"official","title":"Game","brand":"Brand",
               "page_url":"https://brand.test/download.html","original_page_url":"https://brand.test/download.html",
               "filename":"haison_trial.zip","mirrors":[url],"expected_md5":None,"expected_size":None,"fields":{}}
        source=m.definitions()["getchu"]
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);m.initialize(store)
            try:
                index=({}, {m.clean(url):[donor]})
                self.assertEqual(m.match_records(store,source,html,"https://www.getchu.com/soft.phtml?id=1",index),0)
                donor["expected_md5"]="a"*32
                self.assertEqual(m.match_records(store,source,html,"https://www.getchu.com/soft.phtml?id=1",index),1)
                self.assertEqual(json.loads(store.rows("getchu")[0]["data"])["expected_md5"],"a"*32)
            finally:store.db.close()

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
    def test_series_heading_preserves_individual_episode_titles(self):
        from portals import parse_nekoneko
        rows=parse_nekoneko(Path("tests/fixtures/nekoneko-skyfish.html").read_text(),"http://web.archive.org/web/20241107213402/http://nekoneko-web.multi-band.net/nekoneko-web/mirror/sky-fish/sky-fish_dl-menu.html")
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]["title"],"星空TeaParty〜第三話「さよなら」と少女は言った〜")
        self.assertEqual(rows[2]["title"],"星空TeaParty〜第一話「冒険」始めました〜")
        self.assertEqual(rows[0]["filename"],"hosisoraTP03.zip")
        self.assertEqual(rows[0]["expected_md5"],"c9ad656a090a8d490cbe7d9ca14be393")
        self.assertTrue(all(r["release_name"]=="Windows用配信版" for r in rows))
        self.assertTrue(all(r["expected_size"] is None for r in rows))

    def test_unquoted_product_keeps_each_file_checksum(self):
        from portals import parse_nekoneko
        html=Path("tests/fixtures/nekoneko-zweitrial.html").read_text()
        rows=parse_nekoneko(html,"http://web.archive.org/web/20240907105822/http://nekoneko-web.multi-band.net/nekoneko-web/mirror/circle/circlegames/dl-menu/zweitrial_dl-menu.html")
        self.assertEqual(len(rows),3)
        self.assertEqual({r["title"] for r in rows},{"ElementEnsemble -Wind of Moon-"})
        self.assertEqual({r["brand"] for r in rows},{"Zwei Trial"})
        self.assertEqual({r["filename"]:r["expected_md5"] for r in rows},{
            "ee_prom.wmv":"d9058b213ccbc290df30a1e6d1cf7213",
            "ee_gamedemo1.wmv":"ddd4b05c670b82276acbe082f9aec50a",
            "ee_gamedemo2.wmv":"d8387983c87394f37fd250a0a248cfcf"})
        self.assertEqual(rows[0]["release_name"],"プロモーションムービー")
        self.assertTrue(all(r["expected_size"] is None for r in rows))

    def test_legacy_square_product_heading(self):
        from portals import parse_nekoneko
        html=Path("tests/fixtures/nekoneko-aria.html").read_text()
        rows=parse_nekoneko(html,"http://web.archive.org/web/20231207030816/http://nekoneko-web.multi-band.net/nekoneko-web/movie/ani-demo/dl-menu/aria_dl-menu.html")
        self.assertEqual(len(rows),1)
        row=rows[0]
        self.assertEqual(row["title"],"夜明け前より瑠璃色な -Brighter than dawning blue-")
        self.assertEqual(row["filename"],"PS2_d_blue.LZH")
        self.assertEqual(row["expected_md5"],"f579673350a20daeaa015d12277819df")
        self.assertEqual(row["release_name"],"デモムービー")
        self.assertEqual(row["size_text"],"70.86 MB")
        self.assertIsNone(row["expected_size"])
        self.assertIn("/20231207030816id_/",row["mirrors"][0])

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
    def test_rounded_legacy_sizes_are_not_exact_constraints(self):
        from portals import parse_d_station
        rows=parse_d_station(Path("tests/fixtures/d-station-200406.html").read_text(),"https://web.archive.org/web/20040604204931/http://mirror.studio-ramble.com:80/")
        self.assertEqual(len(rows),5)
        self.assertEqual(rows[0]["brand"],"Alster")
        self.assertEqual(rows[0]["expected_md5"],"8ff5a5cfb3f2acbac2447cb1760af1d6")
        self.assertEqual(rows[0]["size_text"],"36MB")
        trial=next(r for r in rows if r["title"]=="3days" and r["release_name"]=="体験版")
        self.assertEqual(trial["expected_md5"],"f863b7266605b69af05b340b77fe8917")
        self.assertEqual(trial["published_at"],"2004-05-01")
        self.assertTrue(all(r["expected_size"] is None for r in rows))
        self.assertTrue(all(r["fields"]["size_is_rounded"] for r in rows))
        self.assertIn("download.php?cid=",rows[0]["fields"]["download_endpoint"])

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
        self.assertEqual(rows[2]['fields']['recovery_urls'],['http://kokoron.madoka.org/mirror/d/denpanodorei/denpa_try.zip'])

    def test_published_sha256_is_checked(self):
        from test_archiver import PAYLOAD
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'file.zip';path.write_bytes(PAYLOAD)
            with self.assertRaisesRegex(ValueError,'SHA-256 mismatch'):a.validate(path,{'expected_sha256':'f'*64})


class DmmTests(unittest.TestCase):
    def test_explicit_trial_uses_product_brand_not_sidebar(self):
        from portals import parse_dmm
        html='<h1>作品A</h1><a href="/list/?maker=2" title="ブランド「ブランドA」へのリンク">ブランドA</a><a href="/list/?maker=9">別ブランド</a><a href="https://sample9.dmm.co.jp/digital/pcgame/brand_0001/brand_0001t.zip">体験版</a><a href="https://sample9.dmm.co.jp/full.zip">購入</a><a href="https://evil.test/trial.zip">体験版</a>'
        rows=parse_dmm(html,'https://dlsoft.dmm.co.jp/detail/brand_0001/')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['brand'],'ブランドA')
        self.assertEqual(rows[0]['filename'],'brand_0001t.zip')
        self.assertEqual(parse_dmm(html,'https://dlsoft.dmm.co.jp/'),[])


class Fuzzy2CatalogueTests(unittest.TestCase):
    page='http://web.archive.org/web/20030101224448/http://mirror.fuzzy2.com/distributes_jp/main.html'

    def test_actual_catalogue_keeps_each_file_hash_and_publisher(self):
        from portals import parse_fuzzy2
        html=Path('tests/fixtures/fuzzy2-distributes-2003.html').read_text()
        rows=parse_fuzzy2(html,self.page)
        self.assertEqual(len(rows),10)
        files={r['filename']:r for r in rows}
        self.assertEqual(files['toon_op.exe']['brand'],'TEATIME')
        self.assertEqual(files['toon_op.exe']['expected_md5'],'db5f5b48443b5c20f3156bfcfc97fe4b')
        self.assertEqual(files['maw3_demo.avi']['brand'],'Studio e.go!')
        self.assertEqual(files['maw3_demo.avi']['expected_md5'],'9ee7fa5c071445fe146a6cfcab3a9085')
        self.assertEqual(files['solfeggio.wma']['expected_md5'],'b65cf84ab013182a1579d3dcd51b53e1')
        self.assertEqual(files['sirotume_ch.lzh']['brand'],'LittleWitch')
        self.assertEqual(files['sirotume_ch.lzh']['release_name'],'体験版')
        self.assertEqual(files['sirotume_ch.lzh']['published_at'],'2002-12-17')
        self.assertEqual(len(files['toon_op.exe']['mirrors']),4)
        self.assertTrue(all(r['expected_size'] is None for r in rows))

    def test_no_page_wide_hash_or_publisher_fallback(self):
        from portals import parse_fuzzy2
        html=Path('tests/fixtures/fuzzy2-distributes-2003.html').read_text()
        without=html.replace('db5f5b48443b5c20f3156bfcfc97fe4b','unavailable')
        self.assertNotIn('toon_op.exe',{r['filename'] for r in parse_fuzzy2(without,self.page)})
        self.assertEqual(parse_fuzzy2(html,self.page.replace('mirror.fuzzy2.com/','example.com/')),[])
