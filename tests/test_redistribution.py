import unittest
from portals import parse_redistribution

PAGE='http://web.archive.org/web/20110829094642/http://www.redistribution.cc/studio-jyaren'
HTML='''<div class="banner"><div class="banner_date">STUDIO邪恋</div>
<div class="banner_date">操心術0</div>
<table><tr><td>タイトル</td><td>体験版</td></tr>
<tr><td>公開日(当サイト)</td><td>2010年12月29日</td></tr>
<tr><td>公開終了日(当サイト)</td><td>-</td></tr>
<tr><td>ファイルサイズ</td><td>470MB</td></tr>
<tr><td>md5</td><td>c1b79bcf1f0d79ec2ec8102f884f8575</td></tr>
<tr><td>ダウンロード</td><td><a href="/contents/2010/12/studio-jyaren/soushin0-taiken.zip">soushin0-taiken.zip</a></td></tr></table>
<div class="banner_date">ゴニン！？</div>
<table><tr><td>タイトル</td><td>デモムービー</td></tr>
<tr><td>公開日(当サイト)</td><td>2010年04月04日</td></tr>
<tr><td>公開終了日(当サイト)</td><td>2010年04月06日</td></tr>
<tr><td>md5</td><td>65d155abc8abd7f526049428b6dfb63f</td></tr>
<tr><td>ダウンロード</td><td>goninmovi.zip</td></tr></table></div>
<aside><a href="/contents/2010/ad.zip">advert</a></aside>'''

class RedistributionTests(unittest.TestCase):
    def test_product_sections_preserve_own_metadata_and_capture(self):
        trial,oldmovie=parse_redistribution(HTML,PAGE)
        self.assertEqual((trial['brand'],trial['title'],trial['release_name']),('STUDIO邪恋','操心術0','体験版'))
        self.assertEqual(trial['expected_md5'],'c1b79bcf1f0d79ec2ec8102f884f8575')
        self.assertEqual(trial['published_at'],'2010-12-29')
        self.assertIsNone(trial['expected_size'])
        self.assertEqual(trial['size_text'],'470MB')
        self.assertEqual(trial['mirrors'][0],'http://web.archive.org/web/20110829094642id_/http://www.redistribution.cc/contents/2010/12/studio-jyaren/soushin0-taiken.zip')
        self.assertEqual(oldmovie['title'],'ゴニン！？')
        self.assertEqual(oldmovie['filename'],'goninmovi.zip')
        self.assertEqual(oldmovie['mirrors'],[])
        self.assertTrue(oldmovie['fields']['source_link_missing'])
        self.assertEqual(oldmovie['fields']['配布終了日'],'2010年04月06日')

    def test_incomplete_identity_or_checksum_is_not_guessed(self):
        self.assertEqual(parse_redistribution(HTML.replace('class="banner_date"','class="other"'),PAGE),[])
        self.assertEqual(len(parse_redistribution(HTML.replace('c1b79bcf1f0d79ec2ec8102f884f8575','bad'),PAGE)),1)
        self.assertEqual(parse_redistribution(HTML,'http://other.test/studio-jyaren'),[])

    def test_download_subdomain_keeps_published_port_and_capture(self):
        raw='http://dl.redistribution.cc:8008/contents/2010/12/studio-jyaren/soushin0-taiken.zip'
        for link in [raw,'https://web.archive.org/web/20150120074941/'+raw]:
            with self.subTest(link=link):
                html=HTML.replace('/contents/2010/12/studio-jyaren/soushin0-taiken.zip',link)
                trial=parse_redistribution(html,PAGE)[0]
                # Wayback rewrites href dates; resolve within the captured
                # document just as other mirror links do. CDX tracks alternatives.
                stamp='20110829094642'
                scheme='http'
                self.assertEqual(trial['mirrors'],[scheme+'://web.archive.org/web/'+stamp+'id_/'+raw,raw])
                self.assertFalse(trial['fields']['source_link_missing'])
                self.assertEqual(trial['expected_md5'],'c1b79bcf1f0d79ec2ec8102f884f8575')

    def test_lookalike_download_domains_are_not_used(self):
        html=HTML.replace('/contents/2010/12/studio-jyaren/soushin0-taiken.zip','http://dl.redistribution.cc.evil.test:8008/contents/2010/12/studio-jyaren/soushin0-taiken.zip')
        trial=parse_redistribution(html,PAGE)[0]
        self.assertEqual(trial['mirrors'],[])
