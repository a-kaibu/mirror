import tempfile
import json
import unittest
from pathlib import Path
import archiver as a
import mirror_sources as m
import portals

PAGE='http://web.archive.org/web/20120503221242/http://www.xgamedata.com/index_m.html'
HTML="""<table><tr><td><a href="demo6/g6361/"><img alt="detail"></a></td>
<td>『作品Ａ』<br>体験版 32.1MB<br><a href="http://publisher.test/">ブランドＡ</a><br>Copyright Publisher</td>
<td><a href="http://xes.plala.jp/demo6/g6361/trial.zip"><img></a></td>
<td><a href="http://xes1.plala.jp/demo6/g6361/trial.zip"><img></a></td></tr></table>"""


class XgameIndexTests(unittest.TestCase):
    def test_explicit_row_keeps_title_brand_release_and_both_dated_mirrors(self):
        rows=portals.parse_xgame(HTML,PAGE);self.assertEqual(len(rows),1);row=rows[0]
        self.assertEqual((row['title'],row['brand'],row['filename']),('作品A','ブランドA','trial.zip'))
        self.assertEqual(row['release_name'],'体験版 32.1MB')
        self.assertEqual(row['fields']['detail_page_url'],'http://www.xgamedata.com/demo6/g6361/')
        self.assertEqual(len(row['mirrors']),4);self.assertIn('/web/20120503221242id_/',row['mirrors'][0])
        self.assertIsNone(row['expected_md5']);self.assertIsNone(row['expected_size'])
        self.assertEqual(row['fields']['official_site_urls'],['http://publisher.test/'])

    def test_unknown_builds_in_different_captures_keep_separate_observations(self):
        old=portals.parse_xgame(HTML,PAGE)[0]
        later=portals.parse_xgame(HTML,PAGE.replace('20120503221242','20130503221242'))[0]
        self.assertNotEqual(old['id'],later['id'])
        self.assertEqual(old['original_page_url'],later['original_page_url'])
        self.assertIn('/web/20130503221242id_/',later['mirrors'][0])

    def test_missing_or_ambiguous_publisher_does_not_guess_a_brand(self):
        cases=[HTML.replace('<a href="http://publisher.test/">ブランドＡ</a>','ブランドＡ'),
               HTML.replace('Copyright Publisher','<a href="http://other.test/">別ブランド</a>')]
        for text in cases:
            with self.subTest(text=text):self.assertEqual(portals.parse_xgame(text,PAGE),[])

    def test_unrelated_binary_paths_and_banner_hosts_are_ignored(self):
        for text in (HTML.replace('/g6361/trial.zip','/g0000/trial.zip'),HTML.replace('xes.plala.jp','banner.test').replace('xes1.plala.jp','banner2.test')):
            self.assertEqual(portals.parse_xgame(text,PAGE),[])

    def test_nested_outer_rows_do_not_combine_multiple_games(self):
        second=HTML.replace('g6361','g6362').replace('作品Ａ','作品Ｂ')
        rows=portals.parse_xgame('<table><tr><td>'+HTML+second+'</td></tr></table>',PAGE)
        self.assertEqual(len(rows),2);self.assertEqual({r['title'] for r in rows},{'作品A','作品B'})
        self.assertEqual(len({r['id'] for r in rows}),2)

    def test_index_identity_does_not_overwrite_a_richer_detail_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                row=portals.parse_xgame(HTML,PAGE)[0]
                import hashlib
                detail_id=hashlib.sha256(('xgame\n'+row['fields']['detail_page_url']+'\ntrial.zip').encode()).hexdigest()
                detail=dict(row,id=detail_id,page_url=row['fields']['detail_page_url'],expected_md5='a'*32)
                m.add_record(store,detail);m.add_record(store,row)
                self.assertEqual(store.db.execute('SELECT count(*) FROM items').fetchone()[0],2)
                saved=json.loads(store.db.execute('SELECT data FROM items WHERE id=?',(detail_id,)).fetchone()[0])
                self.assertEqual(saved['expected_md5'],'a'*32)
                self.assertNotEqual(row['id'],detail_id)
            finally:store.db.close()

    def test_other_hosts_and_rows_without_a_detail_link_are_not_catalogues(self):
        self.assertEqual(portals.parse_xgame(HTML,PAGE.replace('www.xgamedata.com','unrelated.test')),[])
        self.assertEqual(portals.parse_xgame(HTML.replace('href="demo6/g6361/"','href="/news.html"'),PAGE),[])


if __name__=='__main__':unittest.main()
