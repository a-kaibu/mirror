import unittest
from hanny import links
from catalog import release_info

PAGE='https://hannylaboratory.blogspot.com/2023/02/blog-post_9.html'

class HannyTests(unittest.TestCase):
    def test_article_body_links_only_and_version_specific_selection(self):
        one='https://drive.google.com/file/d/1bcLyijIpn660cqhAFwxqQZ9O6ES-AKrP/view'
        two='https://drive.google.com/file/d/12wx5HR41ZDwzFTJqjWevmIbACzQQ4FkU/view'
        html=f'<div class="post-body entry-content"><a href="{one}">４１．ダウンロード</a><a href="{two}">４２．ダウンロード</a></div><div class="comments"><a href="https://drive.google.com/file/d/otherfile123456/view">コメント</a></div>'
        self.assertEqual(links(html,PAGE,{'page':PAGE,'label':'41'}),[(one,'41.ダウンロード')])
        self.assertEqual(links(html,PAGE,{'page':PAGE,'label':'42'}),[(two,'42.ダウンロード')])
        self.assertEqual(links(html,'https://evil.test/',{'page':PAGE}),[])

    def test_full_game_is_not_a_trial_or_patch(self):
        kind,version,_,_=release_info({'title':'ランス4','filename':'rance4.zip','release_name':'本編 ver2.05','fields':{'distribution_kind':'full','version':'2.05'}})
        self.assertEqual((kind,version),('full','2.05'))
