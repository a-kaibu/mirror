import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
import archiver as a
from catalog import release_info
from fuzzy2 import parse,checksum,collect
from test_archiver import Fetch,Response

PAGE='http://web.archive.org/web/20021004110802/http://mirror.fuzzy2.com/typemoon/index.html'
HTML='''<p>TYPE-MOON「幻視同盟」吉里吉里評価版</p><table>
<tr><td>帯域</td><td>ブロードバンド</td><td>ナローバンド</td></tr>
<tr><td>ファイルサイズ</td><td>20.6MB</td><td>9.6MB</td></tr>
<tr><td>他バージョンとの差異</td><td>44.1kHz/ステレオ</td><td>22kHz/モノラル</td></tr>
<tr><td>実行ファイル</td><td><a href="evaluation_full.exe">DL</a></td><td><a href="evaluation_mini.exe">DL</a></td></tr>
<tr><td>MD5ファイル</td><td><a href="evaluation_full.exe.md5">MD5</a></td><td><a href="evaluation_mini.exe.md5">MD5</a></td></tr>
</table><table><tr><td>※注意事項※ 月姫本編の未プレイ者はご遠慮ください。</td></tr></table>'''

class Fuzzy2Tests(unittest.TestCase):
    def test_column_variants_keep_their_own_sidecars_and_audio_specs(self):
        full,mini=parse(HTML,PAGE)
        self.assertEqual(full['filename'],'evaluation_full.exe')
        self.assertEqual(mini['fields']['音声仕様'],'22kHz/モノラル')
        self.assertTrue(mini['fields']['published_checksum_url'].endswith('/evaluation_mini.exe.md5'))
        self.assertIsNone(full['expected_md5'])
        self.assertIsNone(full['expected_size'])
        self.assertIn('月姫本編',full['fields']['適用条件'])
        self.assertEqual(release_info(full)[0],'trial')
        self.assertEqual(len(parse(HTML.replace('evaluation_full.exe.md5','unrelated.md5'),PAGE)),1)

    def test_checksum_is_bound_to_exact_document_and_cached_with_provenance(self):
        url='http://web.archive.org/web/20021004110802id_/http://mirror.fuzzy2.com/typemoon/evaluation_full.exe.md5'
        actual='http://web.archive.org/web/20021009081704id_/http://mirror.fuzzy2.com:80/typemoon/evaluation_full.exe.md5'
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                result=checksum(store,Fetch([Response(b'83e2ba8b6018d89d69d7740559e6eb65',url=actual)]),url)
                self.assertEqual(result[0],'83e2ba8b6018d89d69d7740559e6eb65')
                self.assertEqual(result[1],actual)
                never=Mock();self.assertEqual(checksum(store,never,url),result);never.open.assert_not_called()
                self.assertTrue((store.root/store.db.execute('select path from pages').fetchone()[0]).exists())
            finally:store.db.close()

    def test_invalid_checksum_records_failure_without_registering_unchecked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                path=Path(tmp)/'source.html';path.write_text(HTML)
                store.db.execute("insert into pages(url,site,path,status) values(?,'fuzzy2','source.html','ok')",(PAGE,));store.db.commit()
                record=parse(HTML,PAGE)[0];url=record['fields']['published_checksum_url']
                fetch=Fetch([Response(b'<html>not found</html>',url=url),Response(b'bad',url=url)])
                self.assertEqual(collect(store,fetch),(0,2))
                self.assertEqual(store.db.execute('select count(*) from items').fetchone()[0],0)
                self.assertEqual(store.db.execute("select count(*) from fuzzy2_checksums where status='failed'").fetchone()[0],2)
            finally:store.db.close()

    def test_unchanged_capture_pages_do_not_oscillate_item_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                (Path(tmp)/'source.html').write_text(HTML)
                pages=[PAGE,PAGE.replace('20021004110802','20021207194919')]
                responses=[]
                for page in pages:
                    store.db.execute("insert into pages(url,site,path,status) values(?,'fuzzy2','source.html','ok')",(page,))
                    for record in parse(HTML,page):responses.append(Response(b'1'*32,url=record['fields']['published_checksum_url']))
                store.db.commit()
                self.assertEqual(collect(store,Fetch(responses)),(4,0))
                before=[r['data'] for r in store.rows()]
                never=Mock();self.assertEqual(collect(store,never),(0,0));never.open.assert_not_called()
                self.assertEqual([r['data'] for r in store.rows()],before)
                # A missing derived row can still be rebuilt from cached proof.
                store.db.execute('delete from item_catalog');store.db.execute('delete from items');store.db.commit()
                self.assertEqual(collect(store,never),(2,0));never.open.assert_not_called()
            finally:store.db.close()
