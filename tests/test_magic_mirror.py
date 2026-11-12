import tempfile
import unittest
import archiver as a
import mirror_sources as m
from portals import parse_magic_mirror

PAGE='http://web.archive.org/web/20090220083439/http://magics.ddo.jp/info/dieselmine/kankin_trial.html'
HTML='''<div id="left"><h2>ブランド</h2><p>Wrong sidebar brand</p><h2>MD5</h2><p>ffffffffffffffffffffffffffffffff</p></div>
<div id="right"><h2>素材タイトル</h2><p>監禁少女【re:union】 体験版</p>
<h2>ブランド</h2><p>ディーゼルマイン</p><h2>作品</h2><p>監禁少女【re:union】</p>
<h2>公式サイト</h2><p><a href="https://publisher.test/game"><img src="official.gif"></a></p>
<h2>ダウンロード</h2><p><a href="/data/pub/dieselmine/CejEokGqx6luVQ8ECwUG/kankin_trial.zip"><img src="dl.gif"></a>89.97 MB</p>
<h2>MD5</h2><p>34c5c2c10ded06da5b0a95e8122f8645</p>
<h2>公開開始日(当サイト)</h2><p>2009/02/16 23:20:00</p>
<h2>コメント</h2><p>comment</p><h2>作品</h2><p>Wrong comment product</p></div>'''

class MagicMirrorTests(unittest.TestCase):
    def test_published_headings_bind_download_to_product_not_sidebar_or_comments(self):
        rows=parse_magic_mirror(HTML,PAGE);self.assertEqual(len(rows),1);r=rows[0]
        self.assertEqual(r['title'],'監禁少女【re:union】')
        self.assertEqual(r['brand'],'ディーゼルマイン')
        self.assertEqual(r['filename'],'kankin_trial.zip')
        self.assertEqual(r['expected_md5'],'34c5c2c10ded06da5b0a95e8122f8645')
        self.assertEqual(r['published_at'],'2009-02-16')
        self.assertEqual(r['fields']['official_site_urls'],['https://publisher.test/game'])
        self.assertIsNone(r['expected_size'])
        self.assertEqual(r['size_text'],'89.97 MB')
        self.assertEqual(r['mirrors'][0],'http://web.archive.org/web/20090220083439id_/http://magics.ddo.jp/data/pub/dieselmine/CejEokGqx6luVQ8ECwUG/kankin_trial.zip')

    def test_removed_link_retains_only_previously_observed_checksum_bound_filename(self):
        removed=HTML.replace('<a href="/data/pub/dieselmine/CejEokGqx6luVQ8ECwUG/kankin_trial.zip"><img src="dl.gif"></a>','')
        old=parse_magic_mirror(HTML,PAGE)[0];new=parse_magic_mirror(removed,PAGE)[0]
        self.assertIsNone(new['filename']);self.assertEqual(new['mirrors'],[])
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                m.add_record(store,old);m.add_record(store,new)
                self.assertEqual(new['filename'],'kankin_trial.zip')
                self.assertEqual(new['mirrors'],old['mirrors'])
                self.assertTrue(new['fields']['published_filename_missing'])
                different=parse_magic_mirror(removed.replace('34c5c2c10ded06da5b0a95e8122f8645','f'*32),PAGE)[0]
                m.add_record(store,different)
                self.assertNotEqual(different['id'],old['id'])
                self.assertIsNone(different['filename']);self.assertEqual(different['mirrors'],[])
            finally:store.db.close()

    def test_unrelated_page_and_invalid_publisher_hash_are_not_imported(self):
        self.assertEqual(parse_magic_mirror(HTML,'http://magics.ddo.jp/index.html'),[])
        self.assertEqual(parse_magic_mirror(HTML.replace('34c5c2c10ded06da5b0a95e8122f8645','invalid'),PAGE),[])
