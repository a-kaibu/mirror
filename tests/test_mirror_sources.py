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

    def test_html_batch_commits_sqlite_without_rebuilding_global_json(self):
        with tempfile.TemporaryDirectory() as folder:
            source={**m.definitions()['multi_inside'],'seeds':[],'homes':['http://www.pachi.ac/~multi/']}
            page='https://web.archive.org/web/20190102030405/http://www.pachi.ac/~multi/'
            store=a.Store(folder)
            try:
                m.initialize(store)
                store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES('multi_inside',?,'archive')",(page,));store.db.commit()
                with patch.object(store,'export',side_effect=AssertionError('unexpected complete JSON export')):
                    result=m.crawl(store,source,Fetch([Response(b'<a href="demo.mirror.shtml">demo</a>',url=page)]),max_pages=1)
                self.assertEqual(result,(1,0));self.assertFalse(store.db.in_transaction)
                self.assertEqual(store.db.execute('SELECT status FROM pages').fetchone()[0],'ok')
                self.assertFalse((store.root/'manifest.json').exists())
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

    def test_moepedia_missing_brand_still_preserves_explicit_trial(self):
        html='<h2>作品A</h2><div class="body-shop trial"><h3>体験版</h3><a href="https://sample9.dmm.co.jp/digital/pcgame/a/a.zip">FANZA</a></div>'
        rows=parse_moepedia(html,'https://moepedia.net/game/95566/')
        self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0]['brand'])
        self.assertEqual(rows[0]['title'],'作品A')
        self.assertEqual(rows[0]['filename'],'a.zip')
        self.assertEqual(rows[0]['release_name'],'体験版')
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                store.add(rows[0])
                from catalog import manifest
                data=manifest(store)
                self.assertEqual(data['manufacturers'][0]['name'],'ブランド未確認')
            finally:store.db.close()

    def test_moepedia_without_title_or_explicit_trial_is_not_registered(self):
        page='https://moepedia.net/game/95566/'
        with self.assertRaisesRegex(ValueError,'title missing'):
            parse_moepedia('<div class="trial"><h3>体験版</h3><a href="https://source.test/a.zip">DL</a></div>',page)
        self.assertEqual(parse_moepedia('<h2>作品A</h2><a href="https://source.test/a.zip">購入</a>',page),[])

    def test_moepedia_calendar_stays_within_publisher_selected_years(self):
        page='https://moepedia.net/trial/2026/12/'
        html='<form action="https://moepedia.net/trial"><select name="year"><option value="1992">1992</option><option value="2027">2027</option></select></form><a href="/trial/1991/12/">Previous</a><a href="/trial/2028/1/">Next</a><a href="/trial/1992/1/">Oldest</a><a href="/trial/2027/12/">Future</a><a href="/game/42/">Game</a>'
        source={**m.definitions()['moepedia'],'homes':[page],'seeds':[page]}
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);m.initialize(store)
            try:
                m.crawl(store,source,Fetch([Response(html.encode(),url=page)]),mode='live',max_pages=1)
                urls={r[0] for r in store.db.execute("SELECT url FROM mirror_frontier WHERE status='pending'")}
                self.assertIn('https://moepedia.net/trial/1992/1/',urls)
                self.assertIn('https://moepedia.net/trial/2027/12/',urls)
                self.assertIn('https://moepedia.net/game/42/',urls)
                self.assertNotIn('https://moepedia.net/trial/1991/12/',urls)
                self.assertNotIn('https://moepedia.net/trial/2028/1/',urls)
            finally:store.db.close()

    def test_moepedia_years_are_not_invented_or_taken_from_other_forms(self):
        root=a.DOM('<form action="https://foreign.test/trial"><select name="year"><option value="2000">2000</option></select></form>').root
        self.assertIsNone(m.moepedia_calendar_years(root,'https://moepedia.net/trial/'))
        self.assertTrue(m.moepedia_calendar_allowed('https://moepedia.net/trial/1980/1/',None))
        self.assertFalse(m.moepedia_calendar_allowed('https://moepedia.net/trial/2027/13/',{'2027'}))

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
    def test_old_circle_heading_uses_its_marker_not_a_css_number(self):
        from portals import parse_mimimaid
        html=Path("tests/fixtures/mimimaid-sage-2008.html").read_text()
        rows=parse_mimimaid(html,"http://web.archive.org/web/20080803164204/http://mimimaid.moe.hm/html/x18-doujin/Sage/index.htm")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["brand"],"Sage")
        self.assertEqual(rows[0]["title"],"さま☆ばけ")
        self.assertEqual(rows[0]["expected_size"],25630005)
        self.assertEqual(rows[0]["expected_md5"],"62470d7fcab628dff28e2e7c32502e4b")
        self.assertEqual(rows[0]["published_at"],"2007-08-31")
        self.assertEqual(rows[0]["release_name"],"体験版")
        self.assertEqual(parse_mimimaid(html.replace('■<span class="style4">','<span class="style4">'),"http://mimimaid.moe.hm/html/x18-doujin/Sage/index.htm"),[])

    def test_2008_heading_retains_trial_movie_and_exact_constraints(self):
        from portals import parse_mimimaid
        rows=parse_mimimaid(Path("tests/fixtures/mimimaid-2008.html").read_text(),"http://web.archive.org/web/20081202021820/http://mimimaid.moe.hm:80/html/x18/studio-jyaren/index.htm")
        self.assertEqual(len(rows),2)
        self.assertEqual({r["title"] for r in rows},{"操心術３"})
        self.assertEqual(rows[0]["expected_size"],351440198)
        self.assertEqual(rows[0]["expected_md5"],"7adb230fd28d797ef38c84110e7f527d")
        self.assertEqual(rows[0]["published_at"],"2008-11-27")
        self.assertEqual(rows[1]["expected_size"],112673502)
        self.assertEqual(rows[1]["expected_md5"],"f30c87269c7f552a0b006d0a9e2869a9")
        self.assertIn("体験版",rows[0]["release_name"])
        self.assertIn("ムービー",rows[1]["release_name"])
        html=Path("tests/fixtures/mimimaid-2008.html").read_text().replace('name="upDate" -->---','name="upDate" -->2008/12/01',1)
        updated=parse_mimimaid(html,"http://mimimaid.moe.hm/html/x18/studio-jyaren/index.htm")[0]
        self.assertEqual(updated["published_at"],"2008-11-27")
        self.assertIn("2008/12/01",updated["fields"]["更新情報"])

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


class DlsiteTouchIdentityTests(unittest.TestCase):
    def test_observed_mobile_brand_label_recovers_product_patch(self):
        from portals import parse_dlsite
        # Mobile product pages use dt/dd with ブランド rather than ブランド名.
        maker='Brand A / Brand B'
        url='https://trial.dlsite.com/professional/files/VJ01002283/NARAKU_patch_ver_1_1_0.zip'
        html=('<h1>Game</h1><dl><div class="c-productInfo__item c-productInfo__item--maker">'
              '<dt class="c-productInfo__key">ブランド</dt><dd class="c-productInfo__value">'
              '<a href="/pro-touch/circle/profile/=/maker_id/VG02563.html">'+maker+'</a></dd></div></dl>'
              '<a href="'+url+'">'+url+'</a>')
        rows=parse_dlsite(html,'https://www.dlsite.com/pro-touch/work/=/product_id/VJ01002283.html')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['brand'],maker)
        self.assertEqual(rows[0]['release_name'],'パッチ')
        self.assertEqual(rows[0]['mirrors'],[url])
        self.assertEqual(parse_dlsite(html,'https://www.dlsite.com/pro-touch/work/=/product_id/VJ99999999.html'),[])
    def test_mobile_circle_label_recovers_trial(self):
        from portals import parse_dlsite
        html=('<h1>Game</h1><dl><div><dt>サークル</dt><dd>Circle</dd></div></dl>'
              '<a href="//trial.dlsite.com/doujin/RJ123000/RJ123456_trial.zip">体験版ダウンロード</a>')
        rows=parse_dlsite(html,'https://www.dlsite.com/maniax-touch/work/=/product_id/RJ123456.html')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['brand'],'Circle')
        self.assertEqual(rows[0]['release_name'],'体験版')
    def test_mobile_product_still_requires_maker_identity(self):
        from portals import parse_dlsite
        html='<h1>Game</h1><a href="//trial.dlsite.com/doujin/RJ123000/RJ123456_trial.zip">体験版ダウンロード</a>'
        self.assertEqual(parse_dlsite(html,'https://www.dlsite.com/maniax-touch/work/=/product_id/RJ123456.html'),[])


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
    def test_archived_brand_row_and_trial_link(self):
        from portals import parse_dmm
        page='http://web.archive.org/web/20210130151959/https://dlsoft.dmm.co.jp/detail/sai_0042/'
        html='<h1 id="title" class="item fn">作品</h1><table><tr class="brand"><td class="type">ブランド名：</td><td class="content"><a href="/list/article=maker/id=58685/">LiLinn</a></td></tr></table><a href="https://sample9.dmm.co.jp/digital/pcgame/sai_0042/sai_0042t.zip">体験版ダウンロード</a>'
        rows=parse_dmm(html,page)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['brand'],'LiLinn')
        self.assertEqual(rows[0]['filename'],'sai_0042t.zip')
        self.assertEqual(rows[0]['mirrors'][0],'http://web.archive.org/web/20210130151959id_/https://sample9.dmm.co.jp/digital/pcgame/sai_0042/sai_0042t.zip')
        self.assertEqual(parse_dmm(html.replace('/list/article=maker/id=58685/','https://foreign.test/list/article=maker/id=58685/'),page),[])
        self.assertEqual(parse_dmm(html.replace('ブランド名：','おすすめ：'),page),[])

    def test_conflicting_legacy_and_modern_brands_are_rejected(self):
        from portals import parse_dmm
        html='<h1>作品</h1><a title="ブランド「Other」へのリンク">Other</a><table><tr class="brand"><td>ブランド名：</td><td><a href="/list/article=maker/id=58685/">LiLinn</a></td></tr></table><a href="https://sample9.dmm.co.jp/digital/pcgame/sai_0042/sai_0042t.zip">体験版</a>'
        self.assertEqual(parse_dmm(html,'https://dlsoft.dmm.co.jp/detail/sai_0042/'),[])

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
        self.assertIsNone(next(r for r in parse_fuzzy2(without,self.page) if r['filename']=='toon_op.exe')['expected_md5'])
        self.assertEqual(parse_fuzzy2(html,self.page.replace('mirror.fuzzy2.com/','example.com/')),[])


class Fuzzy2JdmTests(unittest.TestCase):
    page='http://web.archive.org/web/20021228012854/http://mirror.fuzzy2.com:80/jdm/main.html'

    def test_trial_and_hashless_patch_share_paragraph_without_sharing_hash(self):
        from portals import parse_fuzzy2
        from catalog import release_info
        rows=parse_fuzzy2(Path('tests/fixtures/fuzzy2-jdm-2002.html').read_text(),self.page)
        self.assertEqual(len(rows),28)
        files={r['filename']:r for r in rows}
        self.assertEqual(files['imoiko1206.exe']['expected_md5'],'70584e74f2870c32a0c4aed14167d18e')
        patch=files['imoiko_syusei.zip']
        self.assertIsNone(patch['expected_md5'])
        self.assertEqual(patch['brand'],'Overflow')
        self.assertEqual(patch['published_at'],'2002-12-06')
        self.assertEqual(patch['size_text'],'70KB')
        self.assertEqual(patch['release_name'],'旧バージョンからの修正ファイル')
        self.assertEqual(release_info(patch)[0],'patch')
        self.assertTrue(patch['fields']['published_checksum_missing'])
        self.assertNotIn('imoiko.exe',files)
        self.assertTrue(all('imoiko_syusei.zip' in u for u in patch['mirrors']))
        self.assertEqual(files['dcop1.lzh']['expected_md5'],'6f3a21c193de5fb94b3546964a473e2d')
        self.assertEqual(files['dcop2.lzh']['expected_md5'],'ed9afa9fdfb63b992f8a9fa0aea7978a')
        self.assertEqual(files['mitugetu.lzh']['published_at'],'2002-11-06')

    def test_ambiguous_hashes_do_not_inherit_from_other_file(self):
        from portals import parse_fuzzy2
        html=Path('tests/fixtures/fuzzy2-jdm-2002.html').read_text()
        html=html.replace('70584e74f2870c32a0c4aed14167d18e','70584e74f2870c32a0c4aed14167d18e MD5: 11111111111111111111111111111111')
        files={r['filename']:r for r in parse_fuzzy2(html,self.page)}
        self.assertNotIn('imoiko1206.exe',files)
        self.assertIsNone(files['imoiko_syusei.zip']['expected_md5'])

    def test_publisher_copy_paste_links_do_not_receive_another_files_hash(self):
        from portals import parse_fuzzy2
        files={r['filename']:r for r in parse_fuzzy2(Path('tests/fixtures/fuzzy2-jdm-2002.html').read_text(),self.page)}
        row=files['daia_demo.lzh']
        self.assertEqual(row['expected_md5'],'a5a360b7d4bbb698148be43eea2879dd')
        self.assertTrue(all('daia_demo.lzh' in u for u in row['mirrors']))
        self.assertTrue(any('girilove_demo.exe' in u for u in row['fields']['mismatched_file_links']))
        self.assertEqual(files['girilove_demo.exe']['expected_md5'],'69bcdbd0f2eaa019b51b7e5b92643446')


class EarlyMultiInsideCatalogueTests(unittest.TestCase):
    page='http://web.archive.org/web/20020802052401/http://www.pachi.ac/~multi/mirror.shtml'

    def test_real_catalogue_keeps_per_product_identity_and_rounded_sizes(self):
        from portals import parse_multi_inside
        from catalog import release_info
        rows=parse_multi_inside(Path('tests/fixtures/multi-inside-catalogue-2002.html').read_text(),self.page)
        self.assertEqual(len(rows),2)
        files={r['filename']:r for r in rows}
        trial=files['woe_9b.exe'];movie=files['kon_2048.asf']
        self.assertEqual((trial['brand'],trial['title']),('Studio e.go!','エーベンブルグの風'))
        self.assertEqual(movie['brand'],'シュピール')
        self.assertEqual(movie['published_at'],'2002-06-22')
        self.assertEqual(movie['size_text'],'24MB')
        self.assertIsNone(movie['expected_size']);self.assertIsNone(movie['expected_md5'])
        self.assertEqual(release_info(trial)[0],'trial');self.assertEqual(release_info(movie)[0],'movie')
        self.assertEqual(movie['mirrors'][-1],'http://www.pachi.ac/~multi/download/200206/kon_2048.asf')

    def test_reused_basename_does_not_merge_different_source_observations(self):
        from portals import parse_multi_inside
        html=Path('tests/fixtures/multi-inside-catalogue-2002.html').read_text()
        other=html.replace('200206/','200207/').replace('Studio e.go!','Other studio')
        rows=parse_multi_inside(html+other,self.page)
        self.assertEqual(len(rows),4)
        self.assertEqual(len({r['id'] for r in rows}),4)
        self.assertEqual(len(parse_multi_inside(html+html.replace('-2002/06/22-','-2002/07/01-'),self.page)),4)
        self.assertEqual(parse_multi_inside(html,self.page.replace('www.pachi.ac','unrelated.test')),[])

    def test_mixed_category_does_not_turn_the_demo_into_a_trial(self):
        from portals import parse_multi_inside
        from catalog import release_info
        rows=parse_multi_inside(Path('tests/fixtures/multi-inside-catalogue-mixed-2002.html').read_text(),self.page)
        files={r['filename']:r for r in rows}
        self.assertEqual(release_info(files['woe_demo.exe'])[0],'movie')
        self.assertEqual(release_info(files['woe_9b.exe'])[0],'trial')
        self.assertIn('[デモ/体験/Web]',files['woe_demo.exe']['fields']['published_category'])

    def test_legacy_trial_and_movie_formats_are_files(self):
        for url in ['http://kokoron.madoka.org/mirror/r/ryoujokukangofugakuin/rkgr_vista.iso','http://kokoron.madoka.org/mirror/f/futaane/FUTAANE_loveero.msi','http://www.pachi.ac/~multi/download/200206/kon_2048.asf']:
            self.assertIsNotNone(a.EXT.search(url))
        self.assertIsNone(a.EXT.search('http://mirror.test/notes.iso.html'))


class DlsiteMovieTests(unittest.TestCase):
    def test_explicit_product_movie_and_rounded_size(self):
        from portals import parse_dlsite
        html='<h1>Game</h1><table><tr><th>ブランド名</th><td>Brand</td></tr></table><a href="//trial.dlsite.com/professional/VJ004000/VJ003549_trial_mov.zip">ムービーダウンロード (28.54MB)</a>'
        page='https://www.dlsite.com/pro/work/=/product_id/VJ003549.html'
        rows=parse_dlsite(html,page)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['release_name'],'ムービー')
        self.assertEqual(rows[0]['filename'],'VJ003549_trial_mov.zip')
        self.assertEqual(rows[0]['size_text'],'28.54MB');self.assertIsNone(rows[0]['expected_size'])
        self.assertEqual(rows[0]['mirrors'],['https://trial.dlsite.com/professional/VJ004000/VJ003549_trial_mov.zip'])
        self.assertEqual(parse_dlsite(html.replace('VJ003549_trial','VJ999999_trial'),page),[])
        self.assertEqual(parse_dlsite(html.replace('trial.dlsite.com','other.test'),page),[])
        self.assertEqual(parse_dlsite(html.replace('ムービーダウンロード','関連商品'),page),[])

    def test_archived_movie_preserves_observed_capture(self):
        from portals import parse_dlsite
        html='<h1>Game</h1><table><tr><th>ブランド名</th><td>Brand</td></tr></table><a href="http://trial.dlsite.com/professional/VJ004000/VJ003549_trial_mov.zip">ムービーダウンロード</a>'
        page='http://web.archive.org/web/20150102030405/http://www.dlsite.com/pro/work/=/product_id/VJ003549.html'
        row=parse_dlsite(html,page)[0]
        self.assertEqual(row['mirrors'][0],'http://web.archive.org/web/20150102030405id_/http://trial.dlsite.com/professional/VJ004000/VJ003549_trial_mov.zip')


class ImmediateDedicatedCrawlTests(unittest.TestCase):
    def test_crawl_registers_new_distribution_metadata_without_reparse_or_donors(self):
        cases=[
            ('kokoron','https://web.archive.org/web/20080505083448/http://kokoron.madoka.org/mirror/studio-jyaren.html',Path('tests/fixtures/kokoron-2008.html').read_text(),'archive',6),
            ('lapistan','https://web.archive.org/web/20050108213255/http://lapistan.jp/list/PULLTOP/index.htm',Path('tests/fixtures/lapistan-2005.html').read_text(),'archive',4),
            ('dmm','https://dlsoft.dmm.co.jp/detail/brand_0001/','<h1>Game</h1><a title="ブランド「Brand」へのリンク" href="/list/article=maker/id=1/">Brand</a><a href="https://sample2.dmm.co.jp/sample/demo.zip">体験版</a>','live',1),
        ]
        for site,url,html,mode,count in cases:
            with self.subTest(site=site),tempfile.TemporaryDirectory() as root:
                store=a.Store(root)
                try:
                    m.initialize(store)
                    store.db.execute('INSERT INTO mirror_frontier(site,url,mode) VALUES(?,?,?)',(site,url,mode));store.db.commit()
                    processed,found=m.crawl(store,m.definitions()[site],Fetch([Response(html.encode(),url=url)]),mode=mode,max_pages=1)
                    self.assertEqual((processed,found),(1,count))
                    rows=store.db.execute('SELECT data FROM items WHERE site=?',(site,)).fetchall()
                    self.assertEqual(len(rows),count)
                    self.assertTrue(all(json.loads(row[0])['brand'] for row in rows))
                    self.assertEqual(store.db.execute('SELECT status FROM mirror_frontier WHERE site=? AND url=? AND mode=?',(site,url,mode)).fetchone()[0],'captured')
                finally:store.db.close()


class ReparseFrontierStatusTests(unittest.TestCase):
    def test_only_successfully_parsed_saved_review_pages_become_captured(self):
        from mirror_reparse import reparse
        fixture=Path('tests/fixtures/kokoron-2008.html').read_text()
        url='https://web.archive.org/web/20080505083448/http://kokoron.madoka.org/mirror/studio-jyaren.html'
        for html,count,status in [(fixture,6,'captured'),('<html>no distribution metadata</html>',0,'review')]:
            with self.subTest(expected=status),tempfile.TemporaryDirectory() as root:
                store=a.Store(root)
                try:
                    m.initialize(store);path='html/page.html'
                    (Path(root)/'html').mkdir();(Path(root)/path).write_text(html)
                    store.db.execute("INSERT INTO pages(url,site,path,status,response_url) VALUES(?,'kokoron',?,'ok',?)",(url,path,url))
                    store.db.execute("INSERT INTO mirror_frontier(site,url,mode,status,capture_url,path,error) VALUES('kokoron',?,'archive','review',?,?,'unmatched/unsupported catalogue page')",(url,url,path));store.db.commit()
                    self.assertEqual(reparse(store,'kokoron'),(1,count,0))
                    row=store.db.execute('SELECT status,error FROM mirror_frontier').fetchone()
                    self.assertEqual(row['status'],status)
                    if count:self.assertIsNone(row['error'])
                    else:self.assertIsNotNone(row['error'])
                finally:store.db.close()


class GenericAssociationSafetyTests(unittest.TestCase):
    def test_each_mirror_filename_uses_its_own_source_checksum(self):
        html='<table><tr><td><p><a href="trial.zip">Trial</a> MD5: '+('1'*32)+'</p><p><a href="movie.lzh">Wrongly nested link</a></p><table><tr><td><p><a href="movie.lzh">Movie</a> MD5: '+('2'*32)+'</p></td></tr></table></td></tr></table>'
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root);m.initialize(store)
            try:
                base=a.parse_detail(HTML,'https://donor.test/item','d_station')[0]
                donors=[]
                for ident,md5,release in [('trial','1'*32,'体験版'),('movie','2'*32,'ムービー')]:
                    record={**base,'id':ident,'expected_md5':md5,'expected_size':None,'filename':None,'release_name':release}
                    store.add(record);donors.append(record)
                source=m.definitions()['fuzzy2']
                self.assertEqual(m.match_records(store,source,html,'http://mirror.fuzzy2.com/game/index.html',m.known_files(store,'fuzzy2')),2)
                rows={json.loads(row['data'])['filename']:json.loads(row['data']) for row in store.rows('fuzzy2')}
                self.assertEqual(rows['movie.lzh']['expected_md5'],'2'*32)
                self.assertEqual(rows['movie.lzh']['release_name'],'ムービー')
            finally:store.db.close()

    def test_quarantined_association_is_not_a_new_checksum_or_url_donor(self):
        with tempfile.TemporaryDirectory() as root:
            store=a.Store(root)
            try:
                record=a.parse_detail(HTML,'https://donor.test/item','d_station')[0]
                record['fields']['source_association_quarantined']={'reason':'wrong source checksum'}
                store.add(record)
                hashes,urls=m.known_files(store,'fuzzy2')
                self.assertEqual(hashes,{});self.assertEqual(urls,{})
            finally:store.db.close()


class DLsiteDescriptionPatchTests(unittest.TestCase):
    def test_product_bound_url_labels_preserve_each_patch_version(self):
        from portals import parse_dlsite
        html='<h1>Game</h1><table><tr><th>ブランド名</th><td>Brand</td></tr></table>'
        urls=['https://trial.dlsite.com/professional/files/VJ01005081/VBRI_JPR_Patch_'+v+'.zip' for v in ['1.02','1.03','1.04','1.04b']]
        html+=''.join('<a href="'+u+'">'+u+'</a>' for u in urls)
        rows=parse_dlsite(html,'https://www.dlsite.com/pro/work/=/product_id/VJ01005081.html')
        self.assertEqual(len(rows),4)
        self.assertEqual({r['release_name'] for r in rows},{'パッチ'})
        self.assertEqual({r['mirrors'][0] for r in rows},set(urls))
        self.assertTrue(all(r['title']=='Game' and r['brand']=='Brand' and r['expected_md5'] is None for r in rows))

    def test_patch_rejects_other_products_hosts_and_unclassified_bonus_files(self):
        from portals import parse_dlsite
        prefix='<h1>Game</h1><table><tr><th>ブランド名</th><td>Brand</td></tr></table>'
        urls=['https://trial.dlsite.com/professional/files/VJ99999999/game_patch.zip',
              'https://other.test/professional/files/VJ01005081/game_patch.zip',
              'https://trial.dlsite.com/professional/files/VJ01005081/bonus.zip',
              'https://trial.dlsite.com/professional/files/VJ01005081/patchnotes.html',
              'https://trial.dlsite.com/professional/files/VJ01005081/sub/patch.zip']
        html=prefix+'<p>アップデートパッチ</p>'+''.join('<a href="'+u+'">'+u+'</a>' for u in urls)
        self.assertEqual(parse_dlsite(html,'https://www.dlsite.com/pro/work/=/product_id/VJ01005081.html'),[])

    def test_named_patch_and_archived_download_keep_original_filename_and_capture(self):
        from portals import parse_dlsite
        html='<h1>Game</h1><table><tr><th>サークル名</th><td>Brand</td></tr></table><a href="http://trial.dlsite.com/doujin/files/RJ123456/fix.zip">修正ファイル</a>'
        page='http://web.archive.org/web/20150102030405/http://www.dlsite.com/home/work/=/product_id/RJ123456.html'
        rows=parse_dlsite(html,page)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['filename'],'fix.zip')
        self.assertEqual(rows[0]['release_name'],'パッチ')
        self.assertEqual(rows[0]['mirrors'][0],'http://web.archive.org/web/20150102030405id_/http://trial.dlsite.com/doujin/files/RJ123456/fix.zip')


class MimimaidLegacyTests(unittest.TestCase):
    page='http://web.archive.org/web/20071014191233/http://mimimaid.moe.hm/html/x18/light/index_old.htm'

    def file_table(self,filename,digest,size):
        return ('<table width="400"><tr><td>ファイル情報</td><td>解凍前：ＺＩＰアーカイブ</td></tr>'
                '<tr><td>ファイルサイズ</td><td>1 MB ('+str(size)+' バイト)</td></tr>'
                '<tr><td>ＭＤ５</td><td>'+digest+'</td></tr>'
                '<tr><td>ファイル</td><td><a href="src/'+filename+'">Download</a></td></tr></table>')

    def document(self,content):
        return '<html><head><title>light</title></head><body><p>ミラー＞18禁（商業）＞light</p>'+content+'</body></html>'

    def test_submaterials_and_adjacent_products_keep_their_own_hash_and_size(self):
        from portals import parse_mimimaid
        html=self.document('<p>■ 作品A　体験版・主題歌 （\'07/02/18公開）<br>体験版</p>'
            +self.file_table('trial.zip','a'*32,100)+'<p>OP主題歌 ※差し替え</p>'
            +self.file_table('song.zip','b'*32,200)+'<p>■ 作品B　デモムービー （\'06/08/24公開）</p>'
            +self.file_table('movie.zip','c'*32,300))
        rows=parse_mimimaid(html,self.page)
        self.assertEqual([(r['title'],r['release_name'],r['expected_md5'],r['expected_size']) for r in rows],
            [('作品A','体験版','a'*32,100),('作品A','OP主題歌','b'*32,200),('作品B','デモムービー','c'*32,300)])
        self.assertEqual([r['published_at'] for r in rows],['2007-02-18','2007-02-18','2006-08-24'])
        self.assertTrue(all(r['brand']=='light' for r in rows))
        self.assertEqual(rows[0]['mirrors'][0],'http://web.archive.org/web/20071014191233id_/http://mimimaid.moe.hm/html/x18/light/src/trial.zip')

    def test_unknown_heading_does_not_reuse_previous_product(self):
        from portals import parse_mimimaid
        first='<p>■ 作品A　体験版 （2007/02/18公開）</p>'+self.file_table('a.zip','a'*32,100)
        for separator in ['<h2>別作品</h2>','<hr>','<p>関連作品</p>','<p>■ タイトルだけ</p>']:
            rows=parse_mimimaid(self.document(first+separator+self.file_table('other.zip','b'*32,200)),self.page)
            self.assertEqual([r['filename'] for r in rows],['a.zip'])

    def test_ambiguous_file_table_and_invalid_dates_are_not_associated(self):
        from portals import parse_mimimaid
        table=self.file_table('a.zip','a'*32,100).replace('</table>','<tr><td><a href="src/b.zip">Download</a></td></tr></table>')
        html=self.document('<p>■ 作品A　体験版 （2007/02/18公開）</p>'+table)
        self.assertEqual(parse_mimimaid(html,self.page),[])
        html=self.document('<p>■ 作品A　体験版 （2007/02/30公開）</p>'+self.file_table('a.zip','a'*32,100))
        self.assertEqual(parse_mimimaid(html,self.page),[])

    def test_brand_breadcrumb_and_source_host_are_required(self):
        from portals import parse_mimimaid
        html=self.document('<p>■ 作品A　体験版 （2007/02/18公開）</p>'+self.file_table('a.zip','a'*32,100))
        self.assertEqual(parse_mimimaid(html.replace('ミラー＞18禁（商業）＞light','別のページ'),self.page),[])
        self.assertEqual(parse_mimimaid(html,self.page.replace('mimimaid.moe.hm','other.test')),[])


class LapistanPatchListTests(unittest.TestCase):
    def test_mod_page_keeps_each_checksum_and_most_recent_preceding_date(self):
        from portals import parse_lapistan
        html=('<title>ラピスたん　PULLTOP　修正ファイル</title><img alt="PULLTOP">'
              '<td>更新日：2007年2月2日<br><a href="http://mirror1.lapistan.jp:8000/mirror/PULLTOP/mod/haruka.exe">遥かに仰ぎ、麗しの　修正プログラムVer1.01</a> [418KB] MD5：'+('a'*32)
              +'<br>更新日：2005年6月14日<br><a href="http://mirror1.lapistan.jp:8000/mirror/PULLTOP/mod/yunohana101.exe">ゆのはな　修正プログラムVer1.01</a> [7.8MB] MD5：'+('b'*32)+'</td>')
        page='https://web.archive.org/web/20131103175825/http://lapistan.jp/list/PULLTOP/mod.htm'
        rows=parse_lapistan(html,page)
        self.assertEqual([(r['title'],r['expected_md5'],r['published_at']) for r in rows],
                         [('遥かに仰ぎ、麗しの','a'*32,'2007-02-02'),('ゆのはな','b'*32,'2005-06-14')])
        self.assertTrue(all(r['brand']=='PULLTOP' and r['release_name']=='修正プログラムVer1.01' for r in rows))
        self.assertEqual(rows[0]['mirrors'][0],'https://web.archive.org/web/20131103175825id_/http://mirror1.lapistan.jp:8000/mirror/PULLTOP/mod/haruka.exe')

    def test_localized_brand_requires_title_and_explicit_permission_agreement(self):
        from portals import parse_lapistan
        html=('<title>ラピスたん　株式会社エクスペリエンス</title>'
              '<p>当サイトは 株式会社エクスペリエンス の許可を得て掲載しています。</p>'
              '<td>2009年5月21日<br><a href="http://mirror1.lapistan.jp:8000/mirror01/2009/05/experiense/G-XTH3_TRIAL_Install.exe">GENERATION XTH CODE-REALIZE　体験版</a> [177.5MB] MD5：'+('c'*32)+'</td>')
        page='http://lapistan.jp/allage/list/experiense/index.htm'
        row=parse_lapistan(html,page)[0]
        self.assertEqual(row['brand'],'株式会社エクスペリエンス')
        self.assertEqual(row['title'],'GENERATION XTH CODE-REALIZE')
        self.assertEqual(row['release_name'],'体験版')
        self.assertEqual(parse_lapistan(html.replace('当サイトは 株式会社エクスペリエンス','当サイトは 他社'),page),[])
        self.assertEqual(parse_lapistan(html,page.replace('lapistan.jp','other.test')),[])


class TsundereCircleTests(unittest.TestCase):
    def test_circle_patch_retains_title_checksum_exact_size_and_replay(self):
        from portals import parse_tsundere
        html=('<table><tr><th>タイトル</th><td>マジカルバトルアリーナNEXT・魔法少女空閃姫　アップデートVer.1.07a</td></tr>'
              '<tr><th>サークル名</th><td>領域ZERO</td></tr><tr><th>当サイト公開日</th><td>2015-09-01</td></tr>'
              '<tr><th>ファイルサイズ</th><td>226.06MB(237,037,873Byte)</td></tr>'
              '<tr><th>md5/sha1</th><td>c5175a48a7cf2b7b1d0846adf05d288c / 44d993392e47d03bfd9037d5bbeb23e803327786</td></tr>'
              '<tr><th>ダウンロード</th><td><span class="downloadlink"><a href="/download/area-zero/c5175a48a7cf2b7b1d0846adf05d288c/MBAN_M_update_Ver107a.zip">MBAN_M_update_Ver107a.zip</a></span></td></tr></table>')
        page='https://web.archive.org/web/20250621074011/https://mirror.tsundere.ne.jp/mirrorinfo/dojin/consumer/area-zero/MBAN_M-patchv107a.html'
        row=parse_tsundere(html,page)[0]
        self.assertEqual(row['brand'],'領域ZERO')
        self.assertEqual(row['title'],'マジカルバトルアリーナNEXT・魔法少女空閃姫')
        self.assertEqual(row['release_name'],'アップデートVer.1.07a')
        self.assertEqual(row['expected_md5'],'c5175a48a7cf2b7b1d0846adf05d288c')
        self.assertEqual(row['expected_size'],237037873)
        self.assertEqual(row['published_at'],'2015-09-01')
        self.assertEqual(row['mirrors'][0],'https://web.archive.org/web/20250621074011id_/https://mirror.tsundere.ne.jp/download/area-zero/c5175a48a7cf2b7b1d0846adf05d288c/MBAN_M_update_Ver107a.zip')
        self.assertEqual(parse_tsundere(html.replace('サークル名','不明な項目'),page),[])


class MultiInsideJnodeTests(unittest.TestCase):
    page='https://web.archive.org/web/20030402150202/http://www.pachi.ac/~multi/download/index/jnode.html'
    def group(self,title,brand,filename,digest,release='体験版'):
        return ('<a name="20030401">[2003/04/01公開]</a><br>◇ <b>『'+title+'』</b> /(c) <a href="https://publisher.test/">'+brand+'</a> '+release+'：<br>'
                '・<b>'+filename+'</b> ['+'ZIP,26MB] → <a href="http://demomirror.pachi.ac:8000/'+brand+'/'+filename+'">ミラー</a><br>'
                'MD5(<b>'+filename+'</b>) : <b>'+digest+'</b><hr>')

    def test_manufacturers_and_reused_filenames_stay_separate(self):
        from portals import parse_multi_inside
        html='<div>'+self.group('作品A','ブランドA','trial.zip','a'*32)+self.group('作品B','ブランドB','trial.zip','b'*32)+'</div>'
        rows=parse_multi_inside(html,self.page)
        self.assertEqual([(r['title'],r['brand'],r['expected_md5']) for r in rows],[('作品A','ブランドA','a'*32),('作品B','ブランドB','b'*32)])
        self.assertNotEqual(rows[0]['id'],rows[1]['id'])
        self.assertEqual(rows[0]['published_at'],'2003-04-01')
        self.assertEqual(rows[0]['size_text'],'26MB')
        self.assertIsNone(rows[0]['expected_size'])
        self.assertTrue(rows[0]['mirrors'][0].startswith('https://web.archive.org/web/20030402150202id_/'))

    def test_missing_credit_does_not_borrow_another_product_brand(self):
        from portals import parse_multi_inside
        bad=self.group('作品B','ブランドB','trial-b.zip','b'*32).replace('/(c)','')
        html='<div>'+self.group('作品A','ブランドA','trial-a.zip','a'*32)+bad+'</div>'
        self.assertEqual([r['filename'] for r in parse_multi_inside(html,self.page)],['trial-a.zip'])

    def test_filename_specific_hashes_apply_to_trial_and_movie(self):
        from portals import parse_multi_inside
        group=self.group('作品A','ブランドA','trial.zip','a'*32,'デモ/体験版').replace('<hr>','')
        group=group.replace('trial.zip</b> [','trial.zip</b> (体験版) [',1)
        group+='・<b>movie.zip</b> (デモ) [ZIP,29MB] → <a href="http://demomirror.pachi.ac:8000/movie.zip">ミラー</a><br>MD5(<b>movie.zip</b>) : <b>'+('b'*32)+'</b><hr>'
        rows=parse_multi_inside('<div>'+group+'</div>',self.page)
        self.assertEqual([(r['filename'],r['expected_md5'],r['release_name']) for r in rows],[('trial.zip','a'*32,'体験版'),('movie.zip','b'*32,'デモ')])

    def test_conflicting_hash_rejects_only_the_affected_file(self):
        from portals import parse_multi_inside
        bad=self.group('作品A','ブランドA','trial.zip','a'*32).replace('<hr>','MD5(trial.zip) : '+('c'*32)+'<hr>')
        html='<div>'+bad+self.group('作品B','ブランドB','b.zip','b'*32)+'</div>'
        self.assertEqual([r['filename'] for r in parse_multi_inside(html,self.page)],['b.zip'])

    def test_earliest_plain_text_dates_are_also_scoped_to_each_product(self):
        from portals import parse_multi_inside
        html='<div>'+self.group('作品A','ブランドA','a.zip','a'*32)+self.group('作品B','ブランドB','b.zip','b'*32)+'</div>'
        html=html.replace('<a name="20030401">[2003/04/01公開]</a>','[2003/04/01公開]')
        rows=parse_multi_inside(html,self.page)
        self.assertEqual([(r['brand'],r['filename'],r['expected_md5']) for r in rows],[('ブランドA','a.zip','a'*32),('ブランドB','b.zip','b'*32)])


class DmmNavigationTests(unittest.TestCase):
    def test_reviews_are_observed_without_becoming_crawl_targets(self):
        home='https://www.dmm.co.jp/';product=home+'mono/pcgame/-/detail/=/cid=example/'
        review=home+'mono/pcgame/-/detail/review/=/cid=example/?rating=4'
        source={**m.definitions()['dmm'],'seeds':[],'homes':[home]}
        html='<a href="'+product+'">作品</a><a href="'+review+'">レビュー</a>'
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                m.crawl(store,source,Fetch([Response(html.encode(),url=home)]),mode='live',max_pages=1)
                self.assertIsNotNone(store.db.execute("SELECT 1 FROM mirror_frontier WHERE site='dmm' AND url=?",(product,)).fetchone())
                self.assertIsNone(store.db.execute("SELECT 1 FROM mirror_frontier WHERE site='dmm' AND url=?",(review,)).fetchone())
                self.assertIsNotNone(store.db.execute("SELECT 1 FROM mirror_links WHERE site='dmm' AND url=?",(review,)).fetchone())
            finally:store.db.close()

    def test_product_review_query_and_archived_product_remain_navigable(self):
        source=m.definitions()['dmm'];product='https://www.dmm.co.jp/mono/pcgame/-/detail/=/cid=review/?review=1'
        self.assertTrue(m.navigable(source,product))
        self.assertTrue(m.navigable(source,a.replay(product,'20180101000000')))
        self.assertFalse(m.navigable(source,a.replay('https://www.dmm.co.jp/mono/pcgame/-/detail/review/=/cid=test/','20180101000000')))
