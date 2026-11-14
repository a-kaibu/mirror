import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import archiver as a
import staged_archive as staged
from test_archiver import HTML,PAYLOAD
from test_multipart_upload import Gateway as BaseGateway,error


class Gateway(BaseGateway):
    def __init__(self):
        super().__init__();self.copies=[];self.deleted=[];self.lose_copy_ack=False;self.delete_failure=False
    def head_object(self,**kw):
        result=super().head_object(**kw)
        result['ETag']='"'+hashlib.sha256(self.objects[kw['Key']]).hexdigest()+'"'
        return result
    def copy_object(self,**kw):
        self.copies.append(kw);source=kw['CopySource']['Key']
        if self.head_object(Key=source)['ETag']!=kw['CopySourceIfMatch']:raise error('PreconditionFailed')
        self.objects[kw['Key']]=self.objects[source]
        if self.lose_copy_ack:self.lose_copy_ack=False;raise TimeoutError('copy response lost')
        return {}
    def delete_object(self,**kw):
        self.deleted.append(kw['Key'])
        if self.delete_failure:self.delete_failure=False;raise TimeoutError('cleanup unavailable')
        self.objects.pop(kw['Key'],None)


class StagedPromotionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://publisher.test/item','lathercraft')[0]
        self.record['id']='target';self.store.add(self.record)
        self.source=dict(self.row());self.client=Gateway();self.sha=hashlib.sha256(PAYLOAD).hexdigest()
        self.key='files/'+self.sha;self.stage='staging/'+'b'*64;self.reads=[]
        upload=self.client.create_multipart_upload()['UploadId']
        etag=self.client.upload_part(UploadId=upload,PartNumber=1,Body=PAYLOAD,ContentLength=len(PAYLOAD))['ETag']
        self.state=dict(item_id='target',data_sha256=hashlib.sha256(self.source['data'].encode()).hexdigest(),
            staging_key=self.stage,size=len(PAYLOAD),part_size=5*1024**2,source_url='https://source.test/demo.zip',
            nonce='nonce',upload_id=upload,phase='parts')
        self.receipt=dict(nonce='nonce',sha256=self.sha,md5=hashlib.md5(PAYLOAD).hexdigest(),size=len(PAYLOAD),completed_public_object=False,
            parts=[dict(number=1,size=len(PAYLOAD),sha256=self.sha,etag=etag)])
        staged._save(self.store,self.state)
    def row(self):return self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
    def readback(self,key):
        self.reads.append(key);body=self.client.objects[key]
        return dict(sha256=hashlib.sha256(body).hexdigest(),md5=hashlib.md5(body).hexdigest(),size=len(body))
    def promote(self,**kwargs):
        return staged.promote(self.store,self.client,self.source,self.state,self.receipt,readback=kwargs.get('readback',self.readback))

    def test_promotes_and_accepts_only_after_one_full_canonical_read(self):
        result=self.promote();row=self.row()
        self.assertEqual(result['accepted_id'],'target');self.assertFalse(result['cleanup_pending'])
        self.assertEqual(self.reads,[self.key]);self.assertEqual(row['status'],'archived')
        self.assertEqual(row['remote_sha256'],self.sha);self.assertIsNotNone(row['remote_verified_at'])
        self.assertIsNone(row['path']);self.assertEqual(self.client.objects[self.key],PAYLOAD)
        self.assertNotIn(self.stage,self.client.objects);self.assertEqual(self.state['phase'],'cleaned')
        self.assertEqual(self.client.deleted,[self.stage])
        self.assertEqual(staged.journal_path(self.store,'target').stat().st_mode&0o777,0o600)
        self.assertEqual(self.client.copies[0]['CopySourceIfMatch'],'"'+self.sha+'"')

    def test_lost_completion_ack_resumes_completed_staging_without_another_upload(self):
        self.client.fail_completion=True
        with self.assertRaises(TimeoutError):self.promote()
        self.assertEqual(self.row()['status'],'pending');self.assertIn(self.stage,self.client.objects)
        self.promote();self.assertEqual(self.client.created,1);self.assertEqual(self.client.sent,[1])
        self.assertEqual(len(self.client.completed),1);self.assertEqual(self.reads,[self.key])

    def test_lost_copy_ack_resumes_public_object_without_recopy(self):
        self.client.lose_copy_ack=True
        with self.assertRaises(TimeoutError):self.promote()
        self.assertEqual(self.state['phase'],'staged');self.assertIn(self.key,self.client.objects)
        self.promote();self.assertEqual(len(self.client.copies),1);self.assertEqual(self.reads,[self.key])

    def test_bad_public_body_never_assigns_proof_or_cleans_staging(self):
        with self.assertRaisesRegex(ValueError,'canonical public'):
            self.promote(readback=lambda key:dict(sha256='0'*64,md5=self.receipt['md5'],size=len(PAYLOAD)))
        self.assertIsNone(self.row()['remote_verified_at']);self.assertEqual(self.row()['status'],'pending')
        self.assertIn(self.stage,self.client.objects);self.assertEqual(self.client.deleted,[])
        self.promote();self.assertEqual(len(self.client.copies),1)

    def test_receipt_and_server_disagreement_cannot_complete_staging(self):
        upload=self.client.uploads[self.state['upload_id']];upload[1]['ETag']='changed'
        with self.assertRaisesRegex(ValueError,'acknowledgement'):self.promote()
        self.assertEqual(self.client.completed,[]);self.assertIsNone(self.row()['remote_verified_at'])

    def test_bad_publisher_constraint_or_receipt_nonce_fails_before_completion(self):
        for change in [dict(md5='0'*32),dict(nonce='another'),dict(sha256='invalid')]:
            original=self.receipt;self.receipt={**original,**change}
            try:
                with self.subTest(change=change),self.assertRaises(ValueError):self.promote()
            finally:self.receipt=original
        self.assertEqual(self.client.completed,[])

    def test_changed_source_preserves_verified_history_without_updating_new_record(self):
        def changed(key):
            self.store.add({**self.record,'title':'Changed product'})
            return self.readback(key)
        result=self.promote(readback=changed)
        self.assertTrue(result['source_changed']);self.assertNotEqual(result['accepted_id'],'target')
        self.assertIsNone(self.row()['remote_verified_at'])
        history=self.store.db.execute('SELECT * FROM items WHERE id=?',(result['accepted_id'],)).fetchone()
        self.assertEqual(history['remote_sha256'],self.sha)
        self.assertEqual(json.loads(history['data'])['title'],self.record['title'])

    def test_new_quarantine_cannot_reappear_as_downloadable_history(self):
        def changed(key):
            self.store.add({**self.record,'fields':{'source_association_quarantined':True}})
            return self.readback(key)
        result=self.promote(readback=changed)
        history=self.store.db.execute('SELECT data FROM items WHERE id=?',(result['accepted_id'],)).fetchone()
        self.assertTrue(json.loads(history['data'])['fields']['source_association_quarantined'])
        self.assertTrue(all(f['download_url'] is None for f in self.store.export()['files']))

    def test_cleanup_retry_never_refetches_or_rereads_the_body(self):
        self.client.delete_failure=True;result=self.promote()
        self.assertTrue(result['cleanup_pending']);self.assertEqual(self.row()['status'],'archived')
        result=self.promote(readback=Mock(side_effect=AssertionError('body read repeated')))
        self.assertFalse(result['cleanup_pending']);self.assertEqual(self.reads,[self.key])
        self.assertEqual(len(self.client.copies),1);self.assertEqual(self.client.deleted,[self.stage,self.stage])

    def test_existing_canonical_content_is_never_blindly_overwritten(self):
        self.client.objects[self.key]=b'other content'
        with self.assertRaisesRegex(ValueError,'canonical public'):self.promote()
        self.assertEqual(self.client.objects[self.key],b'other content');self.assertEqual(self.client.copies,[])
        self.assertIsNone(self.row()['remote_verified_at'])

    def test_canonical_contention_retains_completed_staging_for_later_promotion(self):
        from archive_lock import exclusive_file
        claim=hashlib.sha256(('s3-upload\n'+'vn-archive\nmirrors\n'+self.key).encode()).hexdigest()
        with exclusive_file(self.store.root/'.claims'/('upload-'+claim+'.lock'),blocking=False):
            with self.assertRaises(BlockingIOError):self.promote()
        self.assertEqual(self.state['phase'],'staged');self.assertIn(self.stage,self.client.objects)
        self.assertIsNone(self.row()['remote_verified_at'])
        self.promote();self.assertEqual(self.client.sent,[1]);self.assertEqual(len(self.client.completed),1)

    def test_existing_local_body_does_not_lose_its_managed_path(self):
        self.source['path']='files/existing.zip'
        with self.assertRaisesRegex(ValueError,'local body'):self.promote()
        self.assertEqual(self.client.completed,[])

    def test_deleted_source_is_not_resurrected(self):
        def deleted(key):
            with self.store.db:
                self.store.db.execute("DELETE FROM item_catalog WHERE item_id='target'")
                self.store.db.execute("DELETE FROM items WHERE id='target'")
            return self.readback(key)
        result=self.promote(readback=deleted)
        self.assertIsNone(result['accepted_id']);self.assertIsNone(self.row())
        self.assertIn(self.key,self.client.objects)
