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
        self.assertEqual([r['filename'] for r in rows],['soushin0-taiken.zip','soushin0_1228.zip'])
        self.assertEqual(rows[0]['mirrors'],[])
        self.assertTrue(rows[0]['fields']['source_link_missing'])
        self.assertFalse(any('different.zip' in u for r in rows for u in r['mirrors']))

    def test_unidentified_brand_or_unrelated_page_is_not_associated(self):
        self.assertEqual(parse_xgame(HTML.replace('このブランド','別のリンク'),PAGE),[])
        self.assertEqual(parse_xgame(HTML,'http://www.xgamedata.com/'),[])

    def test_live_page_preserves_explicit_brand_fragment(self):
        rows=parse_xgame(HTML,'http://www.xgamedata.com/demo5/g5791/')
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['brand'],'STUDIO邪恋')
        self.assertFalse(any('web.archive.org' in u for u in rows[0]['mirrors']))

    def test_removed_brand_navigation_requires_matching_publisher_credits(self):
        import re
        without_navigation=re.sub(r'<a href="http://www.xgamedata.com/ti0/[^>]+>.*?</a>','',HTML)
        credits='<p>Copyright © 2012 ボクカノProject All Rights Reserved.</p><p>※この素材は、 ボクカノ　Ｐｒｏｊｅｃｔ　ｏｎ　ＷＥＢ 様より転載しております。</p>'
        rows=parse_xgame(without_navigation+credits,PAGE)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['brand'],'ボクカノProject')
        self.assertEqual(rows[0]['fields']['brand_evidence']['method'],'copyright_and_attribution')
        self.assertEqual(parse_xgame(without_navigation+credits.replace('2012 ボクカノProject','2012 DifferentPublisher'),PAGE),[])
        self.assertEqual(parse_xgame(without_navigation+'<p>Copyright © 2012 ボクカノProject All Rights Reserved.</p>',PAGE),[])


class LegacyXgameTests(unittest.TestCase):
    def html(self):
        return """<a href="http://www.xgamedata.com/ti0/su.html#ＺＥＲＯ">このブランドの他のタイトルデータを見る</a>
<a href="http://xes.plala.jp/demo2/g1552/game_trial.lzh">体験版</a>
<a href="http://xes1.plala.jp/demo2/g1552/game_trial.lzh">体験版</a>
<a href="http://xes.plala.jp/demo2/g1552/MOV_L.LZH">ムービー</a>
<a href="http://xes.plala.jp/demo2/g1552/MOV_S.LZH">ムービー</a>
<div>【ソフト　　名】『Game』体験版
【登　録　名】game_trial.lzh
【バイト　数】134,615,039byte
【動作　条件】Windows
【転載 条件】著作権者の了承無き転載不可</div>
<div>【ソフト　　名】『Game』高解像度ムービー/低解像度ムービー
【登　録　名】MOV_L.LZH/MOV_S.LZH
【バイト　数】27769131byte/4357297byte
【動作　条件】WMV</div>"""

    def test_exact_bytes_without_md5_and_parallel_movie_variants(self):
        rows=parse_xgame(self.html(),'http://web.archive.org/web/20120514063647/http://www.xgamedata.com/demo2/g1552/')
        self.assertEqual([r['filename'] for r in rows],['game_trial.lzh','MOV_L.LZH','MOV_S.LZH'])
        self.assertEqual([r['expected_size'] for r in rows],[134615039,27769131,4357297])
        self.assertTrue(all(r['expected_md5'] is None for r in rows))
        self.assertEqual(rows[0]['brand'],'ZERO');self.assertEqual(len(rows[0]['mirrors']),4)
        self.assertEqual(rows[1]['release_name'],'高解像度ムービー')
        self.assertEqual(rows[2]['release_name'],'低解像度ムービー')
        self.assertEqual(rows[0]['fields']['適用条件'],'Windows')

    def test_unmatched_size_lists_and_approximate_bytes_are_rejected(self):
        html=self.html().replace('27769131byte/4357297byte','27769131byte')
        self.assertEqual(len(parse_xgame(html,'http://www.xgamedata.com/demo2/g1552/')),1)
        malformed=self.html().replace('134,615,039byte','134,615,039byte/unknown')
        self.assertEqual(len(parse_xgame(malformed,'http://www.xgamedata.com/demo2/g1552/')),2)
        html=self.html().replace('134,615,039byte','約134MB')
        rows=parse_xgame(html,'http://www.xgamedata.com/demo2/g1552/')
        self.assertEqual(len(rows),2);self.assertNotIn('game_trial.lzh',[r['filename'] for r in rows])

    def test_missing_filename_link_retains_metadata_without_guessing_url(self):
        html=self.html().replace('game_trial.lzh">','different.lzh">')
        row=parse_xgame(html,'http://www.xgamedata.com/demo2/g1552/')[0]
        self.assertEqual(row['mirrors'],[]);self.assertTrue(row['fields']['source_link_missing'])

    def test_modern_format_still_requires_its_published_digest(self):
        html=HTML.replace('MD5:8CE2307C23D1E149A679BA89E12C6126','').replace('MD5:73BC3AF33BF4227F2297FD68127393E9','')
        self.assertEqual(parse_xgame(html,PAGE),[])


class LabeledXgameTests(unittest.TestCase):
    def html(self):
        return """<a href="http://www.xgamedata.com/ti0/su.html#ＺＥＲＯ">このブランドの他のタイトルデータを見る</a>
<a href="http://xes.plala.jp/demo5/g5413/mama_demo640.mpg">高画質</a>
<a href="http://xes1.plala.jp/demo5/g5413/mama_demo320.mpg">低画質</a>
<div>【素　材　名】『Game』
デモムービー高解像度版（６４０×４８０）
デモムービー低解像度版（３２０×２４０）
【登　録　名】
デモムービー高解像度版（６４０×４８０）　mama_demo640.mpg
デモムービー低解像度版（３２０×２４０）　mama_demo320.mpg
【ﾌｧｲﾙｻｲｽﾞ/MD5】
デモムービー低解像度版（３２０×２４０）　22.5MB/MD5:74F32DA9BA3C860B2944EB5C5B931FE5
デモムービー高解像度版（６４０×４８０）　46.8MB/MD5:75EA8C6A45BA1E66AA3161EB2D4DC2B2
【動作条件】MPEG再生環境
【転載条件】著作権者の了承無き転載不可</div>"""

    def test_md5_rows_match_quality_labels_even_when_reordered(self):
        rows=parse_xgame(self.html(),'http://www.xgamedata.com/demo5/g5413/')
        self.assertEqual([r['filename'] for r in rows],['mama_demo640.mpg','mama_demo320.mpg'])
        self.assertEqual([r['expected_md5'] for r in rows],['75ea8c6a45ba1e66aa3161eb2d4dc2b2','74f32da9ba3c860b2944eb5c5b931fe5'])
        self.assertEqual([r['size_text'] for r in rows],['46.8MB','22.5MB'])
        self.assertTrue(all(r['expected_size'] is None for r in rows))
        self.assertEqual(rows[0]['title'],'Game')
        self.assertEqual(rows[0]['fields']['適用条件'],'MPEG再生環境')

    def test_incomplete_or_ambiguous_labels_never_borrow_other_md5(self):
        html=self.html();page='http://www.xgamedata.com/demo5/g5413/'
        self.assertEqual(parse_xgame(html.replace('22.5MB/MD5:74F32DA9BA3C860B2944EB5C5B931FE5','missing'),page),[])
        self.assertEqual(parse_xgame(html.replace('22.5MB/MD5:74F32DA9BA3C860B2944EB5C5B931FE5','22.5MB/MD5:74F32DA9BA3C860B2944EB5C5B931FE5\nデモムービー低解像度版（３２０×２４０） 22.5MB/MD5:74F32DA9BA3C860B2944EB5C5B931FE5'),page),[])
        self.assertEqual(parse_xgame(html.replace('mama_demo320.mpg','mama_demo640.mpg'),page),[])

    def test_fullwidth_legacy_separators_preserve_quality_assignment(self):
        html=LegacyXgameTests().html().replace('MOV_L.LZH/MOV_S.LZH','MOV_L.LZH／MOV_S.LZH').replace('27769131byte/4357297byte','27769131byte／4357297byte').replace('高解像度ムービー/低解像度ムービー','高解像度ムービー／低解像度ムービー')
        rows=parse_xgame(html,'http://www.xgamedata.com/demo2/g1552/')
        self.assertEqual(len(rows),3)
        self.assertEqual([r['release_name'] for r in rows[1:]],['高解像度ムービー','低解像度ムービー'])
