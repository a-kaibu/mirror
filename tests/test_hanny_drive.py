import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import archiver as a
import catalog
import hanny_drive as d
from test_archiver import Fetch,Response,PAYLOAD

ENTRY={'key':'daisengoku','page':'https://creator.test/article','title':'戦国ランス','release':'大戦国ランス','creator':'Creator'}
URL='https://drive.google.com/drive/folders/public_folder_id'
FILES=[{'id':'first_public_file','path':'DL版/戦国ランスGA.ald'},{'id':'second_public_file','path':'通常版/戦国ランスGA.ald'}]

class CreatorDriveTests(unittest.TestCase):
    def test_variants_keep_distinct_identity_and_actual_source(self):
        rows=d.records(ENTRY,URL,FILES,{'source_note_sha256':'proof'})
        self.assertNotEqual(rows[0]['id'],rows[1]['id'])
        self.assertEqual(rows[0]['filename'],rows[1]['filename'])
        self.assertEqual([r['fields']['適用条件'] for r in rows],['DL版・廉価版','通常版'])
        self.assertTrue(all(catalog.release_info(r)[0]=='patch' for r in rows))
        self.assertTrue(all('megatools' not in r['fields']['source_integrity'] for r in rows))
        doc=d.records(ENTRY,URL,[{'id':'readme_public_file','path':'Readme.txt'}],{})[0]
        self.assertEqual(catalog.release_info(doc)[0],'other')

    def test_unsafe_or_ambiguous_inventory_is_rejected_before_registration(self):
        for path in ['../outside.zip','/absolute.zip','folder/../../outside.zip','C:\\outside.zip','.']:
            with self.subTest(path=path),self.assertRaises(ValueError):d.records(ENTRY,URL,[{'id':'first_public_file','path':path}],{})
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                with self.assertRaisesRegex(ValueError,'ambiguous'):
                    d.collect(store,ENTRY,URL,[FILES[0],FILES[0]],{},list_only=True)
                self.assertEqual(store.db.execute('SELECT count(*) FROM items').fetchone()[0],0)
            finally:store.db.close()

    def test_sdk_listing_uses_public_anonymous_access_and_does_not_download(self):
        fetch=Mock(return_value=[SimpleNamespace(id='first_public_file',path='mod.zip')])
        with patch.dict('sys.modules',{'gdown':SimpleNamespace(download_folder=fetch)}):
            self.assertEqual(d.list_files(URL),[{'id':'first_public_file','path':'mod.zip'}])
        fetch.assert_called_once_with(url=URL,skip_download=True,quiet=True,use_cookies=False,timeout=30,retries=1)

    def test_discovery_requires_matching_note_receipt_and_unambiguous_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);root=store.root/'mega-mods/daisengoku';root.mkdir(parents=True)
            note=root/'容量制限.txt';note.write_text('GoogleDriveミラー '+URL,encoding='utf-8')
            receipt={'complete':False,'files':{'容量制限.txt':a.digest(note)}}
            (root/'.native-complete.json').write_text(json.dumps(receipt))
            try:
                url,evidence=d.discover(store,'daisengoku')
                self.assertEqual(url,URL)
                self.assertEqual(evidence['source_note_sha256'],a.digest(note)['sha256'])
                note.write_text('changed')
                with self.assertRaisesRegex(ValueError,'differs'):d.discover(store,'daisengoku')
                receipt['files']={'../容量制限.txt':{}}
                (root/'.native-complete.json').write_text(json.dumps(receipt))
                with self.assertRaisesRegex(ValueError,'unsafe'):d.discover(store,'daisengoku')
            finally:store.db.close()

    def test_listing_alone_never_marks_folder_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                result=d.collect(store,ENTRY,URL,FILES,{},list_only=True)
                self.assertEqual(result,{'files':2,'processed':0,'public_verified':0,'status':'partial'})
            finally:store.db.close()

    def test_publication_failure_keeps_partial_state_and_resume_finishes_inventory(self):
        self.check_publication_resume(RuntimeError)

    def test_upload_blocking_error_is_not_mistaken_for_a_busy_file_claim(self):
        self.check_publication_resume(BlockingIOError)

    def check_publication_resume(self,error_type):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);client=Mock();objects={};calls=[]
            client.head_object.return_value={'ContentLength':0}
            def upload(path,bucket,key,**kwargs):
                calls.append(key)
                if len(calls)==2:raise error_type('upload failed')
                objects[key]=Path(path).read_bytes()
            client.upload_file.side_effect=upload
            responses=[Response(payload,headers={'Content-Type':'application/octet-stream'},url='https://drive.usercontent.google.com/download') for payload in [PAYLOAD,PAYLOAD+b'second']]
            fetch=Fetch(responses)
            try:
                with patch.object(a,'Fetcher',return_value=fetch),patch.object(a,'s3_client',return_value=client),patch.object(a,'public_digest',side_effect=lambda ns,bucket,key:(hashlib.sha256(objects[key]).hexdigest(),len(objects[key]))):
                    with self.assertRaisesRegex(error_type,'upload failed'):d.collect(store,ENTRY,URL,FILES,{})
                    self.assertEqual(tuple(store.db.execute('SELECT status,verified_files FROM creator_drive_folders').fetchone()),('partial',1))
                    result=d.collect(store,ENTRY,URL,FILES,{})
                self.assertEqual(result['status'],'complete')
                self.assertEqual(result['public_verified'],2)
                self.assertEqual(len(fetch.requests),2)
                self.assertTrue(all(row['status']=='archived' for row in store.rows()))
            finally:store.db.close()
