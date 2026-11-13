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
    def test_appetite_image_only_trial_and_pv_match_product_number(self):
        page='https://www.appetite-game.com/apt_232.html';game={**GAME,'shoukai':page}
        html='<!-- 体験版 --><div><a href="apt_232/apt232_tr.zip"><img src="apt_232/apt_232_tr_samune.jpg"></a></div><!-- PV --><div><a href="apt_232/apt232_pv.zip"><img src="apt_232/apt_232_pv_bannar.jpg"></a><video src="apt_232/apt232_pv.mp4" controls></video></div>'
        rows,_=o.parse(html,page,game,BRAND,scoped=False)
        self.assertEqual({r['filename']:r['release_name'] for r in rows},{'apt232_tr.zip':'体験版','apt232_pv.zip':'ムービー','apt232_pv.mp4':'ムービー'})
        self.assertTrue(all(r['fields']['publisher_product_link']==page for r in rows))
        self.assertTrue(all(r['expected_md5'] is None for r in rows))
        self.assertEqual(o.parse(html.replace('apt232_','apt233_'),page,game,BRAND,scoped=False)[0],[])
        self.assertEqual(o.parse(html,page,{**game,'shoukai':page.replace('232','233')},BRAND,scoped=False)[0],[])

    def test_appetite_abbreviated_filename_without_published_label_is_not_guessed(self):
        page='https://www.appetite-game.com/apt_232.html';game={**GAME,'shoukai':page}
        html='<a href="apt_232/apt232_tr.zip"><img src="button.jpg"></a>'
        self.assertEqual(o.parse(html,page,game,BRAND,scoped=False)[0],[])

    def test_publisher_wide_patch_retains_published_scope_without_game_assignment(self):
        brand={'id':123,'brandname':'Miel'};page='https://www.miel-soft.com/support.html'
        html='<div class="support"><div><font><b>タイトル名</b>：Mielタイトル全般【2016年より前の作品が対象】<br><b>修正項目</b>：ウィンドウの調整</font></div><div><a href="dl/patch.zip"><img src="download.gif"></a></div></div>'
        rows,_=o.parse(html,page,GAME,brand,scoped=False)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['title'],'Mielタイトル全般【2016年より前の作品が対象】')
        self.assertEqual(rows[0]['fields']['scope_kind'],'publisher')
        self.assertNotIn('erogamescape_game_id',rows[0]['fields'])
        self.assertEqual(rows[0]['release_name'],'パッチ')
        other,_=o.parse(html,page,{**GAME,'id':99,'gamename':'Other'},brand,scoped=False)
        self.assertEqual(rows,other)
        self.assertEqual(o.parse(html.replace('Mielタイトル全般','Otherタイトル全般'),page,GAME,brand,scoped=False)[0],[])
        self.assertEqual(o.parse(html.replace('タイトル全般【2016年より前の作品が対象】','作品A'),page,GAME,brand,scoped=False)[0],[])

    def test_appetite_dash_and_legacy_trial_formats(self):
        for page,assets in [
            ('https://www.appetite-game.com/apadash/aptd_001.html',['aptd_001/aptd001_tr.zip']),
            ('https://www.appetite-game.com/apt_007.html',['apt_007/APT007_tri.zip','apt_007/APT007_tri_exe.zip']),
            ('https://www.appetite-game.com/apt_016.html',['apt_016/apt016_dl/apt016_tr.zip'])]:
            with self.subTest(page=page):
                html='<!-- 体験版 -->'+''.join('<a href="'+u+'"><img src="button.jpg"></a>' for u in assets)
                rows,_=o.parse(html,page,{**GAME,'shoukai':page},BRAND,scoped=False)
                self.assertEqual(len(rows),len(assets))
                self.assertTrue(all(row['release_name']=='体験版' for row in rows))

    def test_appetite_closer_markers_and_observed_same_product_redirect(self):
        page='https://www.appetite-game.com/apt_013.html';requested=page.replace('.html','_kari.html')
        game={**GAME,'shoukai':requested}
        html='<!-- CLOSER|TRIAL --><a href="apt_013/apt013_tr.zip"><img src="button.jpg"></a><!-- CLOSER|PV --><video src="apt_013/apt013_pv.mp4"></video>'
        self.assertEqual(o.parse(html,page,game,BRAND,scoped=False)[0],[])
        rows,_=o.parse(html,page,game,BRAND,scoped=False,requested_page=requested)
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['fields']['official_request_url']==requested for r in rows))
        self.assertEqual(o.parse(html,page,{**game,'shoukai':requested.replace('013','014')},BRAND,scoped=False,requested_page=requested.replace('013','014'))[0],[])

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

    def test_support_patch_uses_explicit_product_url_despite_title_punctuation(self):
        game={**GAME,'shoukai':'https://key.visualarts.co.jp/anemoi/product.html'}
        html='<div class="support"><div><b>修正パッチ公開</b><b>タイトル名</b>：<a href="product.html">Different punctuation</a></div><div><a href="a_patch.zip"><img src="download.gif"></a></div></div><div class="support"><b>修正パッチ</b><b>タイトル名</b>：<a href="other.html">Other</a><a href="b_patch.zip">Download</a></div>'
        rows,_=o.parse(html,PAGE,game,BRAND,scoped=False)
        self.assertEqual([r['filename'] for r in rows],['a_patch.zip'])
        self.assertEqual(rows[0]['fields']['publisher_product_link'],game['shoukai'])
        self.assertEqual(rows[0]['release_name'],'パッチ')
        self.assertIsNone(rows[0]['expected_md5'])

    def test_unlabelled_or_ambiguous_product_links_do_not_attribute_patch(self):
        game={**GAME,'shoukai':'https://key.visualarts.co.jp/anemoi/product.html'}
        for links in ['<a href="product.html">Product</a>','<b>タイトル名</b>：<a href="product.html">Product</a><b>タイトル名</b>：<a href="other.html">Other</a>']:
            rows,_=o.parse('<section>修正パッチ '+links+'<a href="a_patch.zip">Download</a></section>',PAGE,game,BRAND,scoped=False)
            self.assertEqual(rows,[])

    def test_unscoped_download_catalog_requires_product_context(self):
        html='<section><h2>Other title 体験版</h2><a href="other_trial.zip">Download</a></section>'
        records,unknown=o.parse(html,PAGE,GAME,BRAND,scoped=False)
        self.assertEqual(records,[]);self.assertEqual(len(unknown),1)

    def test_checksum_is_not_shared_across_other_files(self):
        html='<main><h2>体験版</h2><p>MD5: '+ 'a'*32 +'</p><a href="first_trial.zip">Download</a><a href="second_trial.zip">Download</a></main>'
        records,_=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(records),2)
        self.assertTrue(all(r["expected_md5"] is None for r in records))

    def test_reused_official_filename_keeps_published_old_build(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);o.initialize(store)
            try:
                def html(digest):
                    return ('<section><h2>体験版</h2><p>MD5: '+digest+'</p><a href="trial.zip">Download</a></section>').encode()
                self.assertEqual(o.scan(store,Fetch([Response(html('a'*32),url=PAGE)]),GAME,BRAND,PAGE),1)
                ident=store.rows()[0]['id']
                store.update(ident,status='archived',md5='a'*32,sha256='1'*64,size=17,remote_sha256='1'*64,remote_key='vn-archive/mirrors/files/'+'1'*64,remote_verified_at=a.now())
                before=dict(store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone())
                self.assertEqual(o.scan(store,Fetch([Response(html('b'*32),url=PAGE)]),GAME,BRAND,PAGE),1)
                self.assertEqual(dict(store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()),before)
                rows=store.rows();self.assertEqual(len(rows),2)
                new=next(row for row in rows if row['id']!=ident)
                data=json.loads(new['data'])
                self.assertEqual(data['expected_md5'],'b'*32)
                self.assertEqual(data['fields']['source_version_of'],ident)
                self.assertEqual(new['status'],'pending')
                self.assertEqual(o.scan(store,Fetch([Response(html('b'*32),url=PAGE)]),GAME,BRAND,PAGE),1)
                self.assertEqual(len(store.rows()),2)
            finally:store.db.close()

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


class GroupedOfficialDistributionTests(unittest.TestCase):
    def test_legacy_update_table_classifies_multiple_patches_without_shared_hash(self):
        html='<table><tr><td><table><tr><td>UPDATE</td></tr></table><a href="build1.zip"><img src="dl.gif"></a><a href="build2.zip"><img src="dl.gif"></a> MD5: '+('a'*32)+'</td></tr></table>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(rows),2);self.assertEqual(unknown,[])
        self.assertTrue(all(row['release_name']=='パッチ' for row in rows))
        self.assertTrue(all(row['expected_md5'] is None for row in rows))
        self.assertTrue(all(row['fields']['official_group_heading']=='UPDATE' for row in rows))

    def test_local_file_hash_is_kept_and_not_copied_to_next_file(self):
        html='<section><h2>修正ファイル</h2><p><a href="build1.zip">Download</a> MD5: '+('a'*32)+'</p><p><a href="build2.zip">Download</a></p></section>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(rows),2);self.assertEqual(unknown,[])
        self.assertEqual(rows[0]['expected_md5'],'a'*32)
        self.assertIsNone(rows[1]['expected_md5'])

    def test_later_trial_heading_does_not_inherit_patch_category(self):
        html='<section><h2>修正ファイル</h2><p><a href="build1.zip">Download</a></p><h2>体験版</h2><p><a href="build2.zip">Download</a></p></section>'
        rows,_=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual([row['release_name'] for row in rows],['パッチ','体験版'])

    def test_unclassified_later_section_and_unscoped_product_remain_unmatched(self):
        html='<section><h2>修正ファイル</h2><p><a href="build1.zip">Download</a></p><h2>FAQ</h2><p><a href="build2.zip">Download</a></p></section>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual([row['filename'] for row in rows],['build1.zip'])
        self.assertEqual(unknown,['https://key.visualarts.co.jp/anemoi/build2.zip'])
        self.assertEqual(o.parse(html,'https://key.visualarts.co.jp/support.html',GAME,BRAND,scoped=False)[0],[])

    def test_explicit_trial_link_inside_update_section_keeps_its_own_kind(self):
        html='<section><h2>UPDATE</h2><a href="trial.zip">体験版</a><a href="build2.zip">Download</a></section>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(unknown,[])
        self.assertEqual([row['release_name'] for row in rows],['体験版','パッチ'])


class LegacyJapaneseOfficialPatchTests(unittest.TestCase):
    def test_cp932_patch_filename_is_classified_without_rewriting_download_url(self):
        from urllib.parse import quote_from_bytes
        name='学園表示バグ修正ファイル.EXE';target='https://key.visualarts.co.jp/anemoi/'+quote_from_bytes(name.encode('cp932'))
        html='<a href="'+target+'">Download</a>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(unknown,[]);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['filename'],name);self.assertEqual(rows[0]['release_name'],'パッチ')
        self.assertEqual(rows[0]['mirrors'],[target])


class OfficialProductHomeTests(unittest.TestCase):
    game={**GAME,'shoukai':'https://publisher.test/game/','trial_url':'https://publisher.test/game/trial.html'}
    trial='https://publisher.test/game/trial.html'
    home='https://publisher.test/game/'
    support='https://publisher.test/game/support.html'

    def responses(self):
        return [Response('<a href="game_trial.zip">体験版</a>'.encode(),url=self.trial),
                Response('<a href="support.html">サポート</a>'.encode(),url=self.home),
                Response('<a href="game_patch.zip">修正パッチ</a>'.encode(),url=self.support)]

    def test_trial_seed_also_follows_declared_home_to_separate_patch_page(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);fetch=Fetch(self.responses())
            try:
                self.assertEqual(o.scan(store,fetch,self.game,BRAND,self.trial),2)
                names={json.loads(row['data'])['filename'] for row in store.rows('official')}
                self.assertEqual(names,{'game_trial.zip','game_patch.zip'})
                self.assertEqual({row[0] for row in store.db.execute('SELECT url FROM official_page_visits WHERE game_id=?',(self.game['id'],))},{self.trial,self.home,self.support})
            finally:store.db.close()

    def test_home_survives_page_bound_and_resume_does_not_repeat_trial(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                responses=self.responses()
                o.scan(store,Fetch(responses[:1]),self.game,BRAND,self.trial,max_pages=1)
                row=store.db.execute('SELECT * FROM official_scans').fetchone()
                errors=json.loads(row['error']);marker=next(x for x in errors if x.get('pending'))
                self.assertEqual([x['url'] for x in marker['pending']],[self.home])
                continuation={**marker,'found':row['found'],'failures':[]}
                self.assertEqual(o.scan(store,Fetch(responses[1:]),self.game,BRAND,self.trial,continuation=continuation),1)
                self.assertEqual(store.db.execute('SELECT found FROM official_scans').fetchone()[0],2)
            finally:store.db.close()

    def test_legacy_home_queue_preserves_existing_review_and_is_not_repeated(self):
        import json
        import official_continue as c
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);o.initialize(store)
            try:
                previous=[{'error':'unclassified binary links need review','urls':['https://publisher.test/game/bonus.zip']}]
                store.db.execute('INSERT INTO official_scans VALUES(?,?,?,?,?,?)',(self.game['id'],self.trial,'review',2,json.dumps(previous),'earlier'));store.db.commit()
                seeds={self.game['id']:(self.game,BRAND,self.trial)}
                self.assertEqual(c.queue_product_homes(store,seeds),1)
                row=store.db.execute('SELECT * FROM official_scans').fetchone();errors=json.loads(row['error'])
                self.assertEqual(errors[0],previous[0]);self.assertEqual(row['found'],2)
                self.assertEqual(errors[1]['pending'],[{'url':self.home,'depth':0,'hints':[]}])
                self.assertEqual(c.queue_product_homes(store,seeds),0)
                store.db.execute('UPDATE official_scans SET error=?',(json.dumps(previous),))
                store.db.execute('INSERT INTO official_page_visits VALUES(?,?,?,?,NULL)',(self.game['id'],self.home,'later','ok'));store.db.commit()
                self.assertEqual(c.queue_product_homes(store,seeds),0)
            finally:store.db.close()


class OfficialImageNavigationTests(unittest.TestCase):
    def test_image_alt_or_link_title_reaches_compact_download_filename(self):
        game={**GAME,'shoukai':'https://publisher.test/game/index.htm'}
        for link in ['<a href="mDl.htm"><img alt="だうんろーど" src="mDlF.gif"></a>',
                     '<a href="mDl.htm" title="Support"><img src="button.gif"></a>',
                     '<a href="mDl.htm"><img alt="ＤＯＷＮＬＯＡＤ" src="button.gif"></a>',
                     '<a href="mDl.htm" title><img alt="だうんろーど" src="button.gif"></a>',
                     '<a href="mDl.htm" title="Support"><img alt src="button.gif"></a>']:
            with self.subTest(link=link),tempfile.TemporaryDirectory() as tmp:
                store=a.Store(tmp)
                fetch=Fetch([Response(link.encode(),url=game['shoukai']),Response('<a href="GamePatch111.exe">修正プログラム</a>'.encode(),url='https://publisher.test/game/mDl.htm')])
                try:
                    self.assertEqual(o.scan(store,fetch,game,BRAND,game['shoukai']),1)
                    self.assertEqual(store.rows('official')[0]['status'],'pending')
                finally:store.db.close()

    def test_explicit_trial_image_label_survives_as_single_file_page_hint(self):
        import json
        game={**GAME,'shoukai':'https://publisher.test/game/index.htm'}
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            fetch=Fetch([Response('<a href="mT.htm"><img alt="体験版" src="button.gif"></a>'.encode(),url=game['shoukai']),Response(b'<a href="program.zip">Download</a>',url='https://publisher.test/game/mT.htm')])
            try:
                self.assertEqual(o.scan(store,fetch,game,BRAND,game['shoukai']),1)
                self.assertEqual(json.loads(store.rows('official')[0]['data'])['release_name'],'体験版')
            finally:store.db.close()

class ImageDistributionClassTests(unittest.TestCase):
    def test_own_image_classes_classify_adjacent_downloads_independently(self):
        html='<div><a href="one.zip"><img class="movie_download" alt title></a></div><div><a href="two.zip"><img class="trial_download"></a></div><div><a href="three.zip"><img id="patch_button"></a></div><div><a href="four.zip"><img class="removie"></a></div>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual({r['filename']:r['release_name'] for r in rows},{'one.zip':'ムービー','two.zip':'体験版','three.zip':'パッチ'})
        self.assertEqual(unknown,['https://key.visualarts.co.jp/anemoi/four.zip'])
        self.assertTrue(all(r['expected_md5'] is None for r in rows))

    def test_shared_publisher_page_still_requires_product_identity(self):
        html='<div><a href="one.zip"><img class="movie_download"></a></div>'
        self.assertEqual(o.parse(html,PAGE,GAME,BRAND,scoped=False)[0],[])

    def test_reused_movie_table_layout_does_not_turn_programs_into_movies(self):
        html='<table class="op_movie_t"><tr><td><a href="enhance106.exe">ダウンロード</a></td></tr></table>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND)
        self.assertEqual(rows,[])
        self.assertEqual(unknown,['https://key.visualarts.co.jp/anemoi/enhance106.exe'])

class PublisherSupportNavigationTests(unittest.TestCase):
    def test_observed_shared_support_page_keeps_product_association(self):
        game={**GAME,'shoukai':'https://publisher.test/game/index.html'}
        start='<a href="/support/game.html">修正パッチ</a><a href="https://other.test/support/game.html">修正パッチ</a><a href="/other/download.html">Download</a>'
        support='<div>anemoi 修正パッチ <a href="/files/game_update.zip">ダウンロード</a></div><div>Another game 修正パッチ <a href="/files/other_update.zip">ダウンロード</a></div>'
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);fetch=Fetch([Response(start.encode(),url=game['shoukai']),Response(support.encode(),url='https://publisher.test/support/game.html')])
            try:
                self.assertEqual(o.scan(store,fetch,game,BRAND,game['shoukai']),1)
                rows=store.rows('official')
                import json
                self.assertEqual(json.loads(rows[0]['data'])['filename'],'game_update.zip')
                self.assertEqual([url for url,_ in fetch.requests],[game['shoukai'],'https://publisher.test/support/game.html'])
            finally:store.db.close()

class SharedSupportProductIdentityTests(unittest.TestCase):
    def test_support_heading_can_identify_the_enclosing_single_file_section(self):
        game={**GAME,'gamename':'夜が来る! -Square of the MOON- Remastered'}
        html='<section><h2>夜が来る！-Square of the MOON- Remastered</h2><div><h4>アップデータVer.1.01</h4><p><a href="NightRemaster_updater_ver101.zip">ダウンロード</a></p></div></section>'
        rows,_=o.parse(html,PAGE,game,BRAND,scoped=False)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['filename'],'NightRemaster_updater_ver101.zip')
        self.assertEqual(rows[0]['release_name'],'パッチ ver101')

    def test_same_filename_in_other_game_section_does_not_borrow_identity(self):
        html='<main><section><h2>anemoi</h2><p><a href="/one/update.zip">修正パッチ</a></p></section><section><h2>Other Game</h2><p><a href="/two/update.zip">修正パッチ</a></p></section></main>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND,scoped=False)
        self.assertEqual([r['mirrors'] for r in rows],[['https://key.visualarts.co.jp/one/update.zip']])
        self.assertEqual(unknown,['https://key.visualarts.co.jp/two/update.zip'])

    def test_title_shortening_or_translation_requires_separate_evidence(self):
        for text in ['Short Title','ムーン・ゴースト']:
            self.assertFalse(o.product_title_present({'gamename':'Moon Ghost Full Title'},text))
