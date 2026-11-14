import unittest
import archiver as a
from portals import parse_nekoneko


class NekonekoMenuTests(unittest.TestCase):
    page='https://web.archive.org/web/20090214230510/http://www.nekoneko-web.net/nekoneko-web18/download/movie/brand/menu.html'
    def html(self,content,holder='ブランド',declared='ブランド'):
        return ('<title>ねこねこ・うぇぶ.net ['+declared+'各作品のムービー／主題歌のダウンロード]</title>'
            '<font color="#000000">'+content+'</font><p>このページに掲載しております各データの著作権は、『'+holder+'』様にあります。</p>')
    def heading(self,title):return '<font color="#0000ff"><b>『'+title+'』</b></font>'
    def link(self,path,label='体験版：12.34 MB'):
        return '<font size="+2"><a href="http://www.nekoneko-web.net/mirror-service/'+path+'">'+label+'</a></font>'
    def test_work_scoped_trials_patches_and_movies_keep_rounded_sizes_without_inventing_md5(self):
        content=self.heading('作品A')+self.link('a/trial.zip')+self.link('a/patch.zip','修正パッチ１.０１：2.36 MB')
        content+=self.heading('作品B')+self.link('b/movie.zip','CMムービー[800x600版]：21.63 MB')
        rows=parse_nekoneko(self.html(content),self.page)
        self.assertEqual([(r['title'],r['brand'],r['filename']) for r in rows],[('作品A','ブランド','trial.zip'),('作品A','ブランド','patch.zip'),('作品B','ブランド','movie.zip')])
        self.assertTrue(all(r['expected_size'] is None and r['expected_md5'] is None for r in rows))
        self.assertTrue(all(r['fields']['size_is_rounded'] for r in rows))
        self.assertTrue(all('/20090214230510id_/' in r['mirrors'][0] for r in rows))
    def test_missing_or_conflicting_brand_evidence_is_not_guessed(self):
        content=self.heading('作品A')+self.link('a/trial.zip')
        self.assertEqual(parse_nekoneko(self.html(content,holder='別ブランド'),self.page),[])
        self.assertEqual(parse_nekoneko(self.html(content).replace('このページに掲載しております各データの著作権は、『ブランド』様にあります。',''),self.page),[])
    def test_unsupported_heading_and_foreign_binary_do_not_inherit_previous_work(self):
        content=self.heading('作品A')+self.link('a/trial.zip')
        content+='<font color="#0000ff">別の作品</font>'+self.link('b/trial.zip')
        content+=self.heading('作品C')+'<a href="https://foreign.test/movie.zip">ムービー：10 MB</a>'
        rows=parse_nekoneko(self.html(content),self.page)
        self.assertEqual([(r['title'],r['filename']) for r in rows],[('作品A','trial.zip')])
    def test_same_filename_in_different_work_paths_and_dated_captures_has_distinct_observations(self):
        content=self.heading('作品A')+self.link('a/trial.zip')+self.heading('作品B')+self.link('b/trial.zip')
        rows=parse_nekoneko(self.html(content),self.page)
        self.assertEqual(len({r['id'] for r in rows}),2)
        older=parse_nekoneko(self.html(content),self.page.replace('20090214230510','20080609011252'))
        self.assertTrue({r['id'] for r in rows}.isdisjoint(r['id'] for r in older))
        self.assertEqual(older[0]['fields']['source_page_capture'],'20080609011252')
    def test_copyright_is_not_authority_for_a_link_outside_its_work_block(self):
        content=self.heading('作品A')+self.link('a/trial.zip')
        html=self.html(content)+'<font color="#000000">'+self.link('ad/extra.zip')+'</font>'
        self.assertEqual([r['filename'] for r in parse_nekoneko(html,self.page)],['trial.zip'])
    def test_published_md5_is_not_silently_ignored_when_its_file_scope_is_unknown(self):
        for label in [' MD5: ','ファイルのMD5値：']:
            content=self.heading('作品A')+self.link('a/trial.zip')+label+('a'*32)
            self.assertEqual(parse_nekoneko(self.html(content),self.page),[])
