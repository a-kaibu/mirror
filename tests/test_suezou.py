import unittest
from portals import parse_suezou

PAGE='https://web.archive.org/web/20071116152855/http://suezou.dyndns.org:80/purple.html'
def block(title,filename,md5,release='ｗｅｂ体験版Ｖｅｒ．２',size='４４４ＭＢ'):
    return f'<table><tr><td><a name="product">{title}</a></td></tr><tr><th>発売日</th><td>２００７年１月１日</td></tr><tr><td><font>Ｐｕｒｐｌｅ ｓｏｆｔｗａｒｅ</font><br>{title}<br>{release}　{size}<br>【著作権 Copyright Purple software All Right Reserved.】<br>{filename}<a href="dl2007/purple/{filename}">ダウンロード</a><br>【 MD5 ({filename}) = {md5} 】</td></tr></table>'

class SuezouTests(unittest.TestCase):
    def test_per_file_metadata_and_observed_replay_link(self):
        rows=parse_suezou(block('作品A','trial.exe','1'*32)+block('作品B','movie.lzh','2'*32,'デモムービー','１１６ＭＢ'),PAGE)
        self.assertEqual(len(rows),2)
        self.assertEqual([r['title'] for r in rows],['作品A','作品B'])
        self.assertEqual([r['expected_md5'] for r in rows],['1'*32,'2'*32])
        self.assertEqual(rows[0]['brand'],'Purple software')
        self.assertEqual(rows[0]['release_name'],'web体験版Ver.2')
        self.assertEqual(rows[0]['size_text'],'444MB');self.assertIsNone(rows[0]['expected_size'])
        self.assertIsNone(rows[0]['published_at'])
        self.assertEqual(rows[0]['mirrors'][0],'https://web.archive.org/web/20071116152855id_/http://suezou.dyndns.org:80/dl2007/purple/trial.exe')

    def test_removed_link_keeps_hash_without_guessing_path(self):
        html=block('作品A','trial.exe','1'*32).replace('<a href="dl2007/purple/trial.exe">ダウンロード</a>','')
        row=parse_suezou(html,PAGE)[0]
        self.assertEqual(row['mirrors'],[]);self.assertTrue(row['fields']['source_link_missing'])

    def test_ambiguous_hash_or_product_context_is_not_assigned(self):
        html=block('作品A','trial.exe','1'*32)
        self.assertEqual(parse_suezou(html.replace('</table>','<tr><td>MD5 (other.exe) = '+('2'*32)+'</td></tr></table>'),PAGE)[0]['expected_md5'],'1'*32)
        self.assertEqual(parse_suezou(html.replace('【 MD5','MD5 (other.exe) = '+('2'*32)+'【 MD5'),PAGE),[])
        self.assertEqual(parse_suezou(html.replace('name="product"','id="product"'),PAGE),[])
        self.assertEqual(parse_suezou(html.replace('Ｐｕｒｐｌｅ ｓｏｆｔｗａｒｅ','Unrelated publisher'),PAGE),[])
        self.assertEqual(parse_suezou(html,'https://unrelated.test/purple.html'),[])

    def test_live_download_uses_published_relative_url(self):
        row=parse_suezou(block('作品A','trial.exe','1'*32),'http://suezou.dyndns.org/purple.html')[0]
        self.assertEqual(row['mirrors'],['http://suezou.dyndns.org/dl2007/purple/trial.exe'])
