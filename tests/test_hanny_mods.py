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

    def test_native_resume_accepts_only_receipted_existing_files(self):
        from hanny_mods import download_native
        import os,subprocess
        if os.name=='nt':self.skipTest('POSIX fixture client')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);staging=root/'staging';staging.mkdir();file=staging/'patch.zip';file.write_bytes(b'unverified old file')
            client=root/'native-client'
            client.write_text("#!/usr/bin/env python3\nimport pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index('--path')+1])/'patch.zip'\nif p.exists():\n print('ERROR: File already exists at '+str(p),file=sys.stderr);sys.exit(1)\np.write_bytes(b'native verified bytes')\nprint(p)\n")
            client.chmod(0o755);receipt=staging/'.native-complete.json';url='https://mega.nz/folder/published#publickey'
            first=download_native(str(client),staging,url,receipt)
            self.assertTrue(first['complete'])
            self.assertEqual(file.read_bytes(),b'native verified bytes')
            second=download_native(str(client),staging,url,receipt)
            self.assertEqual(second['files'],first['files'])
            client.write_text("#!/usr/bin/env python3\nimport sys\nprint('ERROR: transfer quota',file=sys.stderr)\nsys.exit(1)\n")
            with self.assertRaises(subprocess.CalledProcessError):download_native(str(client),staging,url,receipt)
            import json
            self.assertFalse(json.loads(receipt.read_text())['complete'])
