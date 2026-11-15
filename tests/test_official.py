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

    def test_shared_navigation_cannot_assign_another_product_patch(self):
        html='<article><nav>anemoi</nav><section><h2>Other Game</h2><div><p><a href="/other/update.zip">修正パッチ</a></p></div></section></article>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND,scoped=False)
        self.assertEqual(rows,[])
        self.assertEqual(unknown,['https://key.visualarts.co.jp/other/update.zip'])


class OfficialFilenameVersionTests(unittest.TestCase):
    def test_separated_numeric_versions_are_not_truncated(self):
        game={'id':1,'gamename':'Example','shoukai':'https://publisher.test/game/'}
        brand={'id':2,'brandname':'Publisher'}
        for filename,release in [('patch_v1_30.zip','パッチ ver1.30'),('patch_ver1.02.zip','パッチ ver1.02'),('patch_ver101.zip','パッチ ver101'),('patch_v1_30b.zip','パッチ ver1.30b'),('patch_v1_30beta.zip','パッチ')]:
            with self.subTest(filename=filename):
                html='<a href="'+filename+'">Example 修正パッチ</a>'
                records,_=o.parse(html,'https://publisher.test/support/',game,brand,scoped=False)
                self.assertEqual(len(records),1);self.assertEqual(records[0]['release_name'],release)


class ShortTitleAndSharedHomeTests(unittest.TestCase):
    def test_short_japanese_title_in_error_description_is_not_product_evidence(self):
        game={**GAME,'gamename':'あると'}
        html='<p>削除異常であるという警告を修正 <a href="hapi_uninst101.exe">ダウンロード</a></p>'
        rows,unknown=o.parse(html,PAGE,game,BRAND,scoped=False)
        self.assertEqual(rows,[]);self.assertEqual(len(unknown),1)
        for label in ['「あると」修正パッチ','『あると』修正パッチ','あると 修正パッチ']:
            with self.subTest(label=label):
                self.assertEqual(len(o.parse('<p>'+label+'<a href="alto_patch.zip">ダウンロード</a></p>',PAGE,game,BRAND,scoped=False)[0]),1)

    def test_short_ascii_title_does_not_match_inside_another_word(self):
        self.assertFalse(o.product_title_present({'gamename':'ef'},'Different title 修正パッチ'))
        self.assertTrue(o.product_title_present({'gamename':'ef'},'ef 修正パッチ'))

    def test_declared_publisher_home_does_not_scope_every_child_product(self):
        game={**GAME,'shoukai':'https://publisher.test/brand/'}
        brand={**BRAND,'url':'https://publisher.test/brand/index.html'}
        self.assertFalse(o.product_folder_scoped('https://publisher.test/brand/other/support.html',game,brand))
        self.assertFalse(o.product_folder_scoped('https://publisher.test/brand/other/support.html',{**game,'shoukai':brand['url']},brand))
        self.assertTrue(o.product_folder_scoped(PAGE,GAME,BRAND))
        self.assertFalse(o.product_folder_scoped(PAGE,GAME,BRAND,shared_home=True))

    def test_shared_catalogue_home_cannot_assign_another_products_patch(self):
        import json
        game={**GAME,'shoukai':'https://publisher.test/brand/'}
        other={**game,'id':35436,'gamename':'Other Game'}
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);self.addCleanup(store.db.close);o.initialize(store)
            store.db.execute('INSERT INTO external_metadata VALUES(?,?,?,?,?)',('erogamescape','game',other['id'],json.dumps(other),a.now()));store.db.commit()
            html='<section><h2>Other Game 修正パッチ</h2><a href="other_patch.zip">ダウンロード</a></section>'
            fetch=Fetch([Response(html.encode(),url=game['shoukai']+'other/support.html')])
            self.assertEqual(o.scan(store,fetch,game,BRAND,game['shoukai']),0)
            self.assertEqual(store.rows('official'),[])


class PublisherNavigationIdentityTests(unittest.TestCase):
    def test_sidebar_product_image_and_navigation_text_are_not_patch_evidence(self):
        for navigation in ['<div id="supplement"><ul class="quicklink"><li><a href="/anemoi/"><img alt="anemoi"></a></li></ul></div>','<nav>anemoi</nav>','<aside><a href="/anemoi/">anemoi</a></aside>']:
            with self.subTest(navigation=navigation):
                html='<div><div>Other product 修正パッチ<a href="other_patch.zip">ダウンロード</a></div>'+navigation+'</div>'
                rows,unknown=o.parse(html,PAGE,GAME,BRAND,scoped=False)
                self.assertEqual(rows,[]);self.assertEqual(len(unknown),1)

    def test_local_product_heading_image_remains_identity_evidence(self):
        html='<div><h2><img alt="anemoi"></h2><p>修正パッチ <a href="patch.zip">ダウンロード</a></p></div>'
        self.assertEqual(len(o.parse(html,PAGE,GAME,BRAND,scoped=False)[0]),1)


class AliceSupportEditionTests(unittest.TestCase):
    page='https://www.alicesoft.com/support/rance8magnum.html'
    brand={'id':30,'brandname':'ALICESOFT'}
    base={'id':14858,'gamename':'ランス・クエスト','shoukai':'https://www.alicesoft.com/rance8/index.html'}
    html='<section class="support-product"><h2>ランス・クエスト マグナム</h2><div class="cont01"><h3>アップデータVer.2.010</h3><p>"ランス・クエスト マグナム"の更新を行うプログラムです。</p><p>・ランス・クエスト マグナム統合版には適用できません。</p><p><a href="//download.alicesoft.com/rance8/rancequest_updater_ver2010.zip">ダウンロード</a></p></div></section>'

    def test_expansion_target_overrides_base_game_and_keeps_printed_version(self):
        rows,_=o.parse(self.html,self.page,self.base,self.brand,scoped=False)
        self.assertEqual(len(rows),1);record=rows[0]
        self.assertEqual(record['title'],'ランス・クエスト マグナム')
        self.assertNotIn('erogamescape_game_id',record['fields'])
        self.assertEqual(record['release_name'],'パッチ ver2.010')
        self.assertEqual(record['fields']['バージョン'],'2.010')
        self.assertIn('統合版には適用できません',record['fields']['適用条件'])
        self.assertIsNone(record['expected_size'])
        self.assertEqual(record['filename'],'rancequest_updater_ver2010.zip')

    def test_exact_expansion_metadata_keeps_its_game_id(self):
        game={**self.base,'id':16569,'gamename':'ランス・クエスト マグナム'}
        record=o.parse(self.html,self.page,game,self.brand,scoped=False)[0][0]
        self.assertEqual(record['fields']['erogamescape_game_id'],16569)

    def test_faq_append_target_does_not_inherit_the_base_title(self):
        game={'id':1,'gamename':'イブニクル','shoukai':'https://www.alicesoft.com/evenicle/'}
        html='<section class="support-product"><h2>イブニクル</h2><div class="cont01"><h3>アップデータVer.1.01</h3><p>"イブニクル"の更新を行うプログラムです。</p><p><a href="evenicle_updater_ver101.zip">ダウンロード</a></p></div><div class="cont02"><dl class="list-faq"><dt>Windows XP / Vista で"イブニクル外典"が動作しない</dt><dd><p>"イブニクル外典"用のファイルです。"イブニクル"には関係ありません。</p><p><a href="evenicleapc_updater_ver100.zip">ダウンロード</a></p></dd></dl></div></section>'
        rows,_=o.parse(html,'https://www.alicesoft.com/support/evenicle.html',game,self.brand,scoped=False)
        records={r['filename']:r for r in rows};self.assertEqual(len(records),2)
        self.assertEqual(records['evenicle_updater_ver101.zip']['title'],'イブニクル')
        append=records['evenicleapc_updater_ver100.zip'];self.assertEqual(append['title'],'イブニクル外典')
        self.assertNotIn('erogamescape_game_id',append['fields'])
        self.assertIn('には関係ありません',append['fields']['適用条件'])
        self.assertNotIn('バージョン',append['fields'])

    def test_support_layout_on_other_publishers_is_not_treated_as_alicesoft(self):
        self.assertEqual(o.parse(self.html,'https://publisher.test/support/patch.html',GAME,self.brand,scoped=False)[0],[])
        self.assertEqual(o.parse(self.html,self.page,GAME,BRAND,scoped=False)[0],[])


class AliceAmbiguousSupportTests(unittest.TestCase):
    def test_conflicting_explicit_patch_targets_remain_for_review(self):
        html='<section class="support-product"><h2>イブニクル</h2><div class="cont01"><p>"イブニクル"の更新を行うプログラムです。"イブニクル外典"用のファイルです。</p><a href="patch.zip">修正パッチ</a></div></section>'
        game={'id':1,'gamename':'イブニクル'}
        rows,unknown=o.parse(html,'https://www.alicesoft.com/support/evenicle.html',game,{'id':30,'brandname':'ALICESOFT'},scoped=False)
        self.assertEqual(rows,[]);self.assertEqual(len(unknown),1)


class ExplicitResourceLabelTests(unittest.TestCase):
    def test_score_archive_on_a_trial_page_is_not_a_trial(self):
        html='<div class="twitter_box"><h2>ピアノアレンジアルバムの楽譜を公開</h2><table><tr><td class="trial_table_style4"><a href="common/score/score.zip">すべての楽譜PDFデータをダウンロードする (ZIPファイル)</a></td></tr></table></div>'
        rows,unknown=o.parse(html,PAGE,GAME,BRAND,hint='trial')
        self.assertEqual(rows,[]);self.assertEqual(unknown,[])

    def test_trial_layout_style_alone_is_not_semantic_trial_evidence(self):
        html='<table><tr><td class="trial_table_style4"><a href="score.zip">Download</a></td></tr></table>'
        self.assertEqual(o.parse(html,PAGE,GAME,BRAND)[0],[])
        html='<table><tr><td class="trial_table_style4">体験版 <a href="trial.zip">Download</a></td></tr></table>'
        self.assertEqual(o.parse(html,PAGE,GAME,BRAND)[0][0]['release_name'],'体験版')

    def test_official_pamphlet_cannot_borrow_trial_container(self):
        html='<div class="trial"><a href="LLLJ_pamph.zip">オフィシャルパンフレット（PDF版）をダウンロードする</a></div>'
        self.assertEqual(o.parse(html,PAGE,GAME,BRAND,hint='trial')[0],[])

    def test_trial_table_layout_does_not_reclassify_artwork(self):
        html=('<div><h2>体験版＋（演出強化・シーン追加版）</h2>'
            '<div class="trial_table"><table><tr><td><a href="trial.zip">DOWNLOAD</a></td></tr></table></div>'
            '<h2>特典CDアートワーク</h2>'
            '<div class="trial_table"><table><tr><td><a href="artworks.zip">DOWNLOAD</a></td></tr></table></div></div>')
        rows,_=o.parse(html,PAGE,GAME,BRAND,hint='trial')
        self.assertEqual([row['filename'] for row in rows],['trial.zip'])
        self.assertEqual(rows[0]['release_name'],'体験版')

    def test_real_trial_in_shared_layout_and_artwork_bundle_is_kept(self):
        for html in ('<div class="trial_table">体験版<a href="demo.zip">DOWNLOAD</a></div>',
                     '<div class="trial"><a href="demo.zip">アートワーク付き体験版</a></div>'):
            with self.subTest(html=html):
                rows,_=o.parse(html,PAGE,GAME,BRAND)
                self.assertEqual([row['filename'] for row in rows],['demo.zip'])
                self.assertEqual(rows[0]['release_name'],'体験版')

    def test_explicit_resource_image_label_cannot_borrow_trial_container(self):
        for label in ('楽譜','壁紙','取扱説明書'):
            with self.subTest(label=label):
                html='<div class="trial"><a href="asset.zip"><img alt="'+label+'"></a></div>'
                self.assertEqual(o.parse(html,PAGE,GAME,BRAND,hint='trial')[0],[])


class ExplicitManualPatchTests(unittest.TestCase):
    def test_manual_application_patch_is_kept_as_a_patch(self):
        html='<a href="manual_update.zip">Manual patch</a>'
        self.assertEqual(o.parse(html,PAGE,GAME,BRAND)[0][0]['release_name'],'パッチ')


class WaffleSharedSupportTests(unittest.TestCase):
    PAGE='http://www.waffle1999.com/official/patch.html'
    BRAND={'id':10,'brandname':'Waffle'}
    HTML=('<div class="content"><h3>修正・追加パッチ</h3>'
        '<div class="article"><h5>Other Game プラグイン小説版</h5>'
        '<ul><li><p><a href="/s_file/other_nov.zip">バージョンアップ ダウンロード</a></p></li></ul></div>'
        '<div class="article"><h5>anemoi 修正パッチ</h5>'
        '<a href="/s_file/own_patch.zip">修正パッチ ダウンロード</a></div></div>')

    def test_publisher_card_titles_override_the_game_that_reached_shared_page(self):
        rows,_=o.parse(self.HTML,self.PAGE,GAME,self.BRAND,scoped=False)
        files={r['filename']:r for r in rows}
        self.assertEqual(files['other_nov.zip']['title'],'Other Game プラグイン小説版')
        self.assertNotIn('erogamescape_game_id',files['other_nov.zip']['fields'])
        self.assertEqual(files['own_patch.zip']['title'],'anemoi')
        self.assertEqual(files['own_patch.zip']['fields']['erogamescape_game_id'],GAME['id'])
        self.assertTrue(all(r['release_name']=='パッチ' for r in rows))
        other,_=o.parse(self.HTML,self.PAGE,{**GAME,'gamename':'Unrelated Game'},self.BRAND,scoped=False)
        self.assertEqual([(r['filename'],r['title']) for r in other],[(r['filename'],r['title']) for r in rows])

    def test_trial_patch_and_split_legacy_update_keep_publisher_scope(self):
        html=('<div class="content"><h3>修正・追加パッチ</h3>'
            '<div class="article"><h5>Trial Game 体験版Ver2 修正パッチ</h5>'
            '<a href="/s_file/try2_s.lzh">修正パッチ ダウンロード</a></div>'
            '<div class="article"><h5>Legacy Game バージョンアップ</h5>'
            '<a href="/s_file/data01.lzh">ダウンロード1/3</a>'
            '<a href="/s_file/data02.lzh">ダウンロード2/3</a>'
            '<a href="/s_file/fix011107.lzh">ダウンロード3/3</a></div></div>')
        rows,_=o.parse(html,self.PAGE,GAME,self.BRAND,scoped=False,hint='trial')
        self.assertEqual([r['filename'] for r in rows],['try2_s.lzh','data01.lzh','data02.lzh','fix011107.lzh'])
        self.assertEqual(rows[0]['title'],'Trial Game 体験版Ver2')
        self.assertTrue(all(r['release_name']=='パッチ' for r in rows))
        self.assertTrue(all('erogamescape_game_id' not in r['fields'] for r in rows))

    def test_other_publishers_do_not_gain_waffle_title_evidence(self):
        for page,brand in [('https://other.test/official/patch.html',self.BRAND),(self.PAGE,{'id':10,'brandname':'Other'})]:
            with self.subTest(page=page,brand=brand):
                rows,_=o.parse(self.HTML,page,{**GAME,'gamename':'Unrelated Game'},brand,scoped=False)
                self.assertEqual(rows,[])

    def test_ambiguous_publisher_card_is_left_unassociated(self):
        html='<div class="article"><h5>Game A</h5><h5>Game B</h5><a href="/s_file/update.zip">修正パッチ</a></div>'
        rows,unknown=o.parse(html,self.PAGE,GAME,self.BRAND,scoped=False)
        self.assertEqual(rows,[]);self.assertEqual(unknown,['http://www.waffle1999.com/s_file/update.zip'])


class WaffleDetailedSupportTests(unittest.TestCase):
    PAGE='http://www.waffle1999.com/official/patch_.html'
    BRAND={'id':10,'brandname':'Waffle'}

    def test_h6_cards_keep_independent_products_and_do_not_inherit_requested_game(self):
        html=('<div class="patch_list" id="61"><h6>Other Game 修正パッチ</h6>'
            '<div class="right"><p><a href="/s_file/other_patch.zip">Other Game 修正パッチ ダウンロード</a></p></div></div>'
            '<div class="patch_list" id="99"><h6>anemoi 修正パッチ</h6>'
            '<a href="/s_file/new_patch4.zip">修正パッチ ダウンロード</a></div>')
        rows,unknown=o.parse(html,self.PAGE,GAME,self.BRAND,scoped=False)
        self.assertEqual(unknown,[])
        self.assertEqual([(r['filename'],r['title']) for r in rows],[('other_patch.zip','Other Game'),('new_patch4.zip','anemoi')])
        self.assertNotIn('erogamescape_game_id',rows[0]['fields'])
        self.assertEqual(rows[1]['fields']['erogamescape_game_id'],GAME['id'])
        self.assertTrue(all(r['release_name']=='パッチ' for r in rows))
        other,_=o.parse(html,self.PAGE,{**GAME,'gamename':'Unrelated'},self.BRAND,scoped=False)
        self.assertEqual([(r['filename'],r['title']) for r in other],[(r['filename'],r['title']) for r in rows])

    def test_ambiguous_h6_and_foreign_publishers_are_not_scoped(self):
        html='<div class="patch_list"><h6>Game A</h6><h6>Game B</h6><a href="/s_file/new_patch4.zip">修正パッチ</a></div>'
        rows,unknown=o.parse(html,self.PAGE,GAME,self.BRAND,scoped=False)
        self.assertEqual(rows,[]);self.assertEqual(unknown,['http://www.waffle1999.com/s_file/new_patch4.zip'])
        valid='<div class="patch_list"><h6>Other Game 修正パッチ</h6><a href="/s_file/new_patch4.zip">修正パッチ</a></div>'
        for page,brand in [('https://other.test/official/patch_.html',self.BRAND),(self.PAGE,{'id':10,'brandname':'Other'})]:
            with self.subTest(page=page,brand=brand):
                rows,_=o.parse(valid,page,GAME,brand,scoped=False)
                self.assertEqual(rows,[])
