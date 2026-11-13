import unittest
import archiver as a
import catalog
from portals import parse_suezou,parse_multi_inside
from test_suezou import block,PAGE


class LegacyMediaTests(unittest.TestCase):
    def test_flv_download_retains_its_own_source_checksum(self):
        html=(block('作品A','VC_demo_f.flv','f4b1b73fd0f268dae4f178c78ffbc158','デモムービー（ｆｌｖ形式）','３３．４ＭＢ')
              +block('作品B','other.wmv','2'*32,'デモムービー','１１６ＭＢ'))
        rows=parse_suezou(html,PAGE)
        self.assertEqual(len(rows),2)
        row=next(row for row in rows if row['filename']=='VC_demo_f.flv')
        self.assertEqual(row['title'],'作品A')
        self.assertEqual(row['expected_md5'],'f4b1b73fd0f268dae4f178c78ffbc158')
        self.assertEqual(row['size_text'],'33.4MB');self.assertIsNone(row['expected_size'])
        self.assertEqual(catalog.release_info(row)[0],'movie')
        self.assertIn('/20071116152855id_/',row['mirrors'][0])
    def test_realmedia_catalogue_keeps_each_published_file_without_fabricated_hash(self):
        page='http://web.archive.org/web/20020802052401/http://www.pachi.ac/~multi/mirror.shtml'
        html="""<div class="main"><div><span class="date">-2002/03/21-</span>
        <span class="color-9">[デモ/Web]</span></div>
        <span class="color-3">[GIRL'S SOFTWARE]</span><span class="color-4">コミック カフェ</span><hr>
        <a href="./download/200202/ccpmoa2.rm">ccpmoa2.rm</a>[RM,3MB] (デモ2)<br>
        <a href="./download/200203/ccop2m.rm">ccop2m.rm</a>[RM,2MB] (デモ3)</div>"""
        rows=parse_multi_inside(html,page)
        self.assertEqual([row['filename'] for row in rows],['ccpmoa2.rm','ccop2m.rm'])
        self.assertEqual([row['size_text'] for row in rows],['3MB','2MB'])
        for row in rows:
            self.assertEqual(row['brand'],"GIRL'S SOFTWARE")
            self.assertEqual(row['title'],'コミック カフェ')
            self.assertEqual(row['published_at'],'2002-03-21')
            self.assertIsNone(row['expected_md5']);self.assertIsNone(row['expected_size'])
            self.assertEqual(catalog.release_info(row)[0],'movie')
            self.assertIn('/20020802052401id_/',row['mirrors'][0])
    def test_extensions_do_not_match_documents_or_other_suffixes(self):
        for url in ['http://mirror.test/movie.flv','http://mirror.test/MOVIE.RM?download=1']:
            self.assertIsNotNone(a.EXT.search(url))
        for url in ['http://mirror.test/movie.flv.html','http://mirror.test/movie.rmdir','http://mirror.test/movie.ram']:
            self.assertIsNone(a.EXT.search(url))


if __name__=='__main__':unittest.main()
