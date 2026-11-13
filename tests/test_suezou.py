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

    def test_localized_publisher_is_confirmed_by_same_block_permission(self):
        html=block('作品A','trial.exe','1'*32).replace('Ｐｕｒｐｌｅ ｓｏｆｔｗａｒｅ','すみっこソフト')
        html=html.replace('Copyright Purple software','Copyright (c)2016 SUMIKKO-SOFTWARE')
        html=html.replace('【 MD5','◆このホームページは、すみっこソフト様より素材使用の許諾、又は転載許可を受けております。<br>【 MD5')
        row=parse_suezou(html,PAGE)[0]
        self.assertEqual(row['brand'],'すみっこソフト')
        self.assertEqual(row['fields']['brand_evidence']['permission_name'],'すみっこソフト')
        self.assertEqual(row['expected_md5'],'1'*32)
        self.assertEqual(row['filename'],'trial.exe')

    def test_permission_from_another_block_cannot_confirm_publisher(self):
        mismatched=block('作品A','trial.exe','1'*32).replace('Copyright Purple software','Copyright Different company')
        permission='◆このホームページは、Purple software様より素材使用の許諾、又は転載許可を受けております。'
        self.assertEqual(parse_suezou(mismatched+'<p>'+permission+'</p>',PAGE),[])
        self.assertEqual(parse_suezou(mismatched.replace('【 MD5',permission.replace('Purple software','Other brand')+'【 MD5'),PAGE),[])
        other=block('作品B','movie.zip','2'*32).replace('【 MD5',permission+'【 MD5')
        rows=parse_suezou(mismatched+other,PAGE)
        self.assertEqual([r['filename'] for r in rows],['movie.zip'])

    def test_conflicting_permission_names_do_not_confirm_localized_publisher(self):
        html=block('作品A','trial.exe','1'*32).replace('Copyright Purple software','Copyright Different company')
        permissions=''.join('このホームページは、'+name+'様より素材使用の許諾。' for name in ['Purple software','Other brand'])
        self.assertEqual(parse_suezou(html.replace('【 MD5',permissions+'【 MD5'),PAGE),[])
