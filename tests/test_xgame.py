import unittest
from portals import parse_xgame

PAGE='http://web.archive.org/web/20120514233142/http://www.xgamedata.com/demo5/g5791/'
HTML='''<title>『操心術０』体験版／ムービー</title>
<a href="http://www.xgamedata.com/ti0/su.html#ＳＴＵＤＩＯ邪恋">このブランドの他のタイトルデータを見る</a>
<a href="http://xes.plala.jp/demo5/g5791/soushin0-taiken.zip"><img src="download.gif"></a>
<a href="http://xes1.plala.jp/demo5/g5791/soushin0-taiken.zip"><img src="download.gif"></a>
<a href="http://xes.plala.jp/demo5/g5791/soushin0_1228.zip"><img src="download.gif"></a>
<div>【素　材　名】　『操心術０』体験版
【登　録　名】　soushin0-taiken.zip
【ﾌｧｲﾙｻｲｽﾞ/MD5】　90.4MB/MD5:8CE2307C23D1E149A679BA89E12C6126
【動作条件】　製品版に準じるものとする
【転載条件】　著作権者の了承無き転載不可</div>
<div>【素　材　名】　『操心術０』デモムービー
【登　録　名】　soushin0_1228.zip
【ﾌｧｲﾙｻｲｽﾞ/MD5】　113MB/MD5:73BC3AF33BF4227F2297FD68127393E9
【動作条件】　ＭＰＥＧ形式のファイルが再生できる環境
【転載条件】　著作権者の了承無き転載不可</div>'''

class XgameTests(unittest.TestCase):
    def test_distribution_sections_keep_own_digest_and_real_mirrors(self):
        trial,movie=parse_xgame(HTML,PAGE)
        self.assertEqual((trial['title'],trial['brand']),('操心術０','STUDIO邪恋'))
        self.assertEqual(trial['release_name'],'体験版')
        self.assertEqual(movie['release_name'],'デモムービー')
        self.assertEqual(trial['expected_md5'],'8ce2307c23d1e149a679ba89e12c6126')
        self.assertEqual(movie['expected_md5'],'73bc3af33bf4227f2297fd68127393e9')
        self.assertEqual(len(trial['mirrors']),4)
        self.assertEqual(trial['mirrors'][0],'http://web.archive.org/web/20120514233142id_/http://xes.plala.jp/demo5/g5791/soushin0-taiken.zip')
        self.assertEqual(trial['size_text'],'90.4MB')
        self.assertIsNone(trial['expected_size'])
        self.assertEqual(movie['fields']['適用条件'],'ＭＰＥＧ形式のファイルが再生できる環境')

    def test_missing_link_does_not_generate_a_binary_url(self):
        html=HTML.replace('soushin0-taiken.zip"><img','different.zip"><img')
        rows=parse_xgame(html,PAGE)
        self.assertEqual([r['filename'] for r in rows],['soushin0_1228.zip'])

    def test_unidentified_brand_or_unrelated_page_is_not_associated(self):
        self.assertEqual(parse_xgame(HTML.replace('このブランド','別のリンク'),PAGE),[])
        self.assertEqual(parse_xgame(HTML,'http://www.xgamedata.com/'),[])

    def test_live_page_preserves_explicit_brand_fragment(self):
        rows=parse_xgame(HTML,'http://www.xgamedata.com/demo5/g5791/')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['brand'],'STUDIO邪恋')
        self.assertFalse(any('web.archive.org' in u for u in rows[0]['mirrors']))
