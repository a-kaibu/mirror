from pathlib import Path
import unittest

from portals import parse_multi_inside
from catalog import release_info


class LegacyMultiInsideTests(unittest.TestCase):
    def fixture(self,name):return (Path(__file__).parent/'fixtures'/('multi-inside-'+name+'-2002.html')).read_text()
    def page(self,name):return 'https://web.archive.org/web/20020803201239/http://www.pachi.ac/~multi/download/index/'+name+'.html'

    def test_actual_2002_trial_and_movie_keep_their_own_checksums(self):
        rows=parse_multi_inside(self.fixture('yumekumi'),self.page('yumekumi'))
        self.assertEqual(len(rows),2);by_name={r['filename']:r for r in rows}
        movie=by_name['ykdemo.lzh'];trial=by_name['yuku_t.lzh']
        self.assertEqual(movie['brand'],'megami');self.assertEqual(movie['title'],'ゆめくみ')
        self.assertEqual(movie['expected_md5'],'94aae62899528b020a74751a6f877aee')
        self.assertEqual(trial['expected_md5'],'a2c8df22b614014109575118fcfc576f')
        self.assertEqual(movie['release_name'],'OPデモ');self.assertEqual(trial['release_name'],'体験版')
        self.assertEqual(release_info(movie)[0],'movie');self.assertEqual(release_info(trial)[0],'trial')
        self.assertEqual(movie['size_text'],'45MB');self.assertIsNone(movie['expected_size'])
        self.assertIn('http://www.pachi.ac:8080/download/200208/ykdemo.lzh',movie['mirrors'])
        self.assertIsNone(movie['published_at'])

    def test_nested_official_brand_and_published_ftp_are_preserved(self):
        rows=parse_multi_inside(self.fixture('ai2'),self.page('ai2'))
        self.assertEqual(len(rows),1);row=rows[0]
        self.assertEqual(row['title'],'魔法少女アイ2');self.assertEqual(row['brand'],'colors')
        self.assertEqual([p['name'] for p in row['fields']['publisher_links']],['hue','colors'])
        self.assertEqual(row['expected_md5'],'1562a98e6db1c2604fe0ca922e038228')
        self.assertIn('http://www.pachi.ac:8000/download/200209/ai2demo_s.mpg',row['mirrors'])
        self.assertEqual(row['fields']['published_ftp_links'][0]['url'],'ftp://www.pachi.ac/download/200209/ai2demo_s.mpg')
        self.assertFalse(any('ftp://' in url for url in row['mirrors']))

    def test_missing_or_conflicting_digest_never_uses_another_files_hash(self):
        html=self.fixture('yumekumi')
        rows=parse_multi_inside(html.replace('a2c8df22b614014109575118fcfc576f',''),self.page('yumekumi'))
        self.assertIsNone(next(r for r in rows if r['filename']=='yuku_t.lzh')['expected_md5'])
        conflict=html.replace('＜補足説明＞','MD5(ykdemo.lzh) = '+'f'*32+'<br>＜補足説明＞')
        rows=parse_multi_inside(conflict,self.page('yumekumi'))
        self.assertEqual([r['filename'] for r in rows],['yuku_t.lzh'])
        self.assertEqual(rows[0]['expected_md5'],'a2c8df22b614014109575118fcfc576f')

    def test_unrelated_credit_holders_are_not_guessed_as_one_subbrand(self):
        html=self.fixture('ai2').replace('http://www.hue.co.jp/index.htm','http://unrelated.test/index.htm')
        row=parse_multi_inside(html,self.page('ai2'))[0]
        self.assertEqual(' '.join(row['brand'].split()),'hue / colors')

    def test_ftp_only_publication_retains_metadata_without_guessing_http_url(self):
        import re
        html=re.sub(r'<a href="[^"]+">ミラー\(HTTP\)</a>','',self.fixture('ai2'))
        row=parse_multi_inside(html,self.page('ai2'))[0]
        self.assertEqual(row['mirrors'],[])
        self.assertEqual(row['filename'],'ai2demo_s.mpg')
        self.assertEqual(row['expected_md5'],'1562a98e6db1c2604fe0ca922e038228')
