import unittest
import archiver as a
import catalog
from portals import parse_multi_inside,parse_suezou
from test_suezou import block,PAGE

MULTI='https://web.archive.org/web/20020802052401/http://www.pachi.ac/~multi/mirror.shtml'

def product(title,links,brand='アージュ'):
    return '<div class="main"><div><span class="date">-2002/04/12-</span><span class="color-9">[体験/Web]</span></div><span class="color-3">['+brand+']</span><span class="color-4">'+title+'</span><hr>'+links+'</div>'

def link(filename,kind='DAT',size='20MB',directory='./download/200204/'):
    return '<a href="'+directory+filename+'">'+filename+'</a>['+kind+','+size+'] '

class SplitDownloadTests(unittest.TestCase):
    def test_split_suffixes_are_bounded_and_case_insensitive(self):
        for name in ['trial.r00','trial.R99','trial.000','trial.001?download=1','trial.999#part']:
            self.assertIsNotNone(a.EXT.search(name),name)
        for name in ['trial.r100','trial.01','trial.0000','trial.r00.html','trial.001.txt','trial.dat','trial.bat']:
            self.assertIsNone(a.EXT.search(name),name)

    def test_each_suezou_part_keeps_its_own_checksum(self):
        html=block('作品A','trial.001','1'*32,'体験版 分割ファイル 1/2','20MB')+block('作品A','trial.002','2'*32,'体験版 分割ファイル 2/2','19MB')
        rows=parse_suezou(html,PAGE)
        self.assertEqual([r['filename'] for r in rows],['trial.001','trial.002'])
        self.assertEqual([r['expected_md5'] for r in rows],['1'*32,'2'*32])
        self.assertEqual([r['size_text'] for r in rows],['20MB','19MB'])
        self.assertTrue(all(catalog.release_info(r)[0]=='trial' for r in rows))

    def test_all_published_rar_volumes_are_kept(self):
        names=['Kiminozo_try.exe']+['Kiminozo_try.r'+str(i).zfill(2) for i in range(7)]
        html=product('君が望む永遠',''.join(link(n,'EXE' if n.endswith('.exe') else 'DAT') for n in names))
        rows=parse_multi_inside(html,MULTI)
        self.assertEqual([r['filename'] for r in rows],names)
        for row in rows:
            self.assertEqual(row['title'],'君が望む永遠');self.assertIsNone(row['expected_md5'])
            self.assertEqual(catalog.release_info(row)[0],'trial')

    def test_join_script_requires_typed_link_and_same_directory_parts(self):
        names=['HIMETAIKEN.000','HIMETAIKEN.001','HIMETAIKEN.bat']
        links=link(names[0])+link(names[1])+link(names[2],'BAT','0.1MB')
        rows=parse_multi_inside(product('姫三妹',links,brand='アンダームーン'),MULTI)
        self.assertEqual([r['filename'] for r in rows],names)
        companion=rows[-1]['fields']['distribution_companion']
        self.assertEqual(companion['role'],'join_script')
        for invalid in [link('HIMETAIKEN.bat','BAT'),link('other.bat','BAT')+link('HIMETAIKEN.000'),link('HIMETAIKEN.bat','EXE')+link('HIMETAIKEN.000'),link('HIMETAIKEN.bat','BAT',directory='./unrelated/')+link('HIMETAIKEN.000')]:
            parsed=parse_multi_inside(product('姫三妹',invalid),MULTI)
            self.assertFalse(any(r['filename'].lower().endswith('.bat') for r in parsed))

    def test_data_companions_require_same_product_installer(self):
        html=product('Peeping Web',link('trial.EXE','EXE')+''.join(link('trial_'+str(i)+'.DAT','DAT','8MB') for i in range(2,11)))
        rows=parse_multi_inside(html,MULTI)
        self.assertEqual(len(rows),10)
        self.assertEqual(rows[-1]['filename'],'trial_10.DAT')
        self.assertEqual(rows[-1]['fields']['distribution_companion']['related_filename'],'trial.EXE')
        self.assertTrue(all(r['expected_md5'] is None for r in rows))
        isolated=product('別作品',link('trial.EXE','EXE'))+product('Peeping Web',link('trial_2.DAT','DAT'))
        self.assertFalse(any(r['filename'].endswith('.DAT') for r in parse_multi_inside(isolated,MULTI)))


if __name__=='__main__':unittest.main()
