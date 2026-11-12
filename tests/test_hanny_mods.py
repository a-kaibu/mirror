from pathlib import Path
import tempfile
import unittest
import archiver as a
from hanny_mods import ingest

class NativeModImportTests(unittest.TestCase):
    def test_import_preserves_relative_paths_and_native_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'download';(folder/'旧版'/'通常版').mkdir(parents=True)
            (folder/'旧版'/'通常版'/'戦国ランス.ain').write_bytes(b'original-script-data')
            (folder/'Readme.txt').write_bytes(b'Original creator readme')
            store=a.Store(root/'archive')
            entry={'key':'test','page':'https://hannylaboratory.blogspot.com/test','title':'戦国ランス','release':'test MOD','creator':'Creator'}
            self.assertEqual(ingest(store,entry,folder,'https://mega.nz/folder/published#publickey',upload=False),2)
            rows=store.rows();self.assertEqual(len(rows),2)
            import json
            relative={json.loads(r['data'])['fields']['mega_relative_path'] for r in rows}
            self.assertEqual(relative,{'Readme.txt','旧版/通常版/戦国ランス.ain'})
            self.assertTrue(all(r['status']=='verified' for r in rows))
            self.assertTrue(all(store.db.execute('SELECT kind FROM item_catalog WHERE item_id=?',(r['id'],)).fetchone()[0]=='patch' for r in rows))
            store.db.close()

    def test_public_import_requires_native_receipt_and_exact_file_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'download';folder.mkdir();file=folder/'patch.zip';file.write_bytes(b'original bytes')
            store=a.Store(root/'archive');entry={'key':'test','page':'https://hannylaboratory.blogspot.com/test','title':'戦国ランス','release':'test MOD','creator':'Creator'}
            url='https://mega.nz/folder/published#publickey'
            with self.assertRaisesRegex(ValueError,'receipt required'):ingest(store,entry,folder,url)
            expected=a.digest(file);file.write_bytes(b'changed bytes')
            with self.assertRaisesRegex(ValueError,'differ from native'):ingest(store,entry,folder,url,receipt={'url':url,'files':{'patch.zip':expected}})
            self.assertEqual(store.rows(),[])
            self.assertTrue(file.exists());store.db.close()
