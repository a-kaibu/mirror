import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError
import archiver as a
from archive_lock import exclusive_file
import multipart_upload as m
from test_archiver import HTML,PAYLOAD


def error(code):return ClientError({'Error':{'Code':code,'Message':'fixture'}},'Fixture')


class Gateway:
    def __init__(self):
        self.uploads={};self.objects={};self.created=0;self.sent=[];self.completed=[]
        self.fail_part=None;self.fail_after_store=False;self.fail_completion=False
        self.page_size=1000;self.markers=[];self.list_error=None;self.bad_etag=False;self.aborts=0
    def head_bucket(self,**kw):return {}
    def upload_file(self,*args,**kw):raise AssertionError('legacy uploader used')
    def create_multipart_upload(self,**kw):
        self.created+=1;ident='opaque-upload-'+str(self.created);self.uploads[ident]={};return {'UploadId':ident}
    def upload_part(self,**kw):
        number=kw['PartNumber'];self.sent.append(number);body=kw['Body'];self.assert_length(body,kw['ContentLength'])
        if number==self.fail_part and not self.fail_after_store:self.fail_part=None;raise TimeoutError('fixture lost connection')
        etag='opaque-part-'+hashlib.sha256(body).hexdigest();self.uploads[kw['UploadId']][number]={'Body':body,'Size':len(body),'ETag':etag,'PartNumber':number}
        if number==self.fail_part:self.fail_part=None;raise TimeoutError('fixture lost acknowledgement')
        if self.bad_etag:self.bad_etag=False;return {}
        return {'ETag':etag}
    def assert_length(self,body,size):
        if len(body)!=size:raise AssertionError('incorrect ContentLength')
    def list_parts(self,**kw):
        if self.list_error:raise error(self.list_error)
        if kw['UploadId'] not in self.uploads:raise error('NoSuchUpload')
        marker=kw.get('PartNumberMarker',0);self.markers.append(marker)
        all_parts=[p for n,p in sorted(self.uploads[kw['UploadId']].items()) if n>marker]
        page=all_parts[:self.page_size]
        result={'Parts':[{k:v for k,v in p.items() if k!='Body'} for p in page],'IsTruncated':len(all_parts)>len(page)}
        if result['IsTruncated']:result['NextPartNumberMarker']=page[-1]['PartNumber']
        return result
    def complete_multipart_upload(self,**kw):
        upload=self.uploads[kw['UploadId']];parts=kw['MultipartUpload']['Parts'];self.completed.append(parts)
        for p in parts:
            if upload[p['PartNumber']]['ETag']!=p['ETag']:raise AssertionError('wrong opaque ETag')
        self.objects[kw['Key']]=b''.join(upload[p['PartNumber']]['Body'] for p in parts);del self.uploads[kw['UploadId']]
        if self.fail_completion:self.fail_completion=False;raise TimeoutError('fixture completion response lost')
        return {}
    def head_object(self,**kw):
        if kw['Key'] not in self.objects:raise error('NoSuchKey')
        return {'ContentLength':len(self.objects[kw['Key']])}
    def abort_multipart_upload(self,**kw):self.aborts+=1


class MultipartTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(lambda:self.store.db.close())
        cache=m.cache_path(self.store.root);cache.parent.mkdir(parents=True,exist_ok=True)
        self.cache=sqlite3.connect(cache);self.cache.row_factory=sqlite3.Row;m.initialize(self.cache);self.addCleanup(lambda:self.cache.close())
        self.body=self.store.root/'body';self.payload=b'abcdefghijklmnopqrstuvwxyz!';self.body.write_bytes(self.payload)
        self.sha=hashlib.sha256(self.payload).hexdigest();self.key='files/'+self.sha;self.client=Gateway()
        patcher=patch.object(m,'MIN_PART_SIZE',1);patcher.start();self.addCleanup(patcher.stop)
    def readback(self):
        body=self.client.objects[self.key];return hashlib.sha256(body).hexdigest(),len(body)
    def send(self,readback=None,part_size=8):
        return m.transfer_and_readback(self.store,self.client,'vn-archive','mirrors',self.key,self.body,self.sha,len(self.payload),readback or self.readback,part_size=part_size)
    def state(self):return self.cache.execute('SELECT * FROM _s3_uploads').fetchone()
    def test_network_failure_preserves_progress_and_reopen_resumes_only_missing_parts(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        self.assertEqual(self.client.sent,[1,2]);self.assertEqual(self.client.aborts,0)
        self.assertEqual(self.cache.execute('SELECT count(*) FROM _s3_upload_parts').fetchone()[0],1)
        self.store.db.close();self.cache.close();self.store=a.Store(self.store.root)
        self.cache=sqlite3.connect(m.cache_path(self.store.root));self.cache.row_factory=sqlite3.Row
        self.assertEqual(self.send(),(self.sha,len(self.payload)))
        self.assertEqual(self.client.sent,[1,2,2,3,4]);self.assertEqual(self.client.created,1)
        self.assertEqual(self.state()['phase'],'completed')
        self.assertEqual([p['PartNumber'] for p in self.client.completed[0]],[1,2,3,4])
    def test_unacknowledged_server_part_is_resent_instead_of_trusted(self):
        self.client.fail_part=2;self.client.fail_after_store=True
        with self.assertRaises(TimeoutError):self.send()
        self.assertIn(2,next(iter(self.client.uploads.values())))
        self.send();self.assertEqual(self.client.sent,[1,2,2,3,4])
    def test_remote_etag_or_size_change_forces_reupload(self):
        for field,value in [('ETag','different-opaque-etag'),('Size',7)]:
            with self.subTest(field=field):
                self.client=Gateway();m.discard(self.store,'vn-archive','mirrors',self.key,self.sha)
                self.client.fail_part=2
                with self.assertRaises(TimeoutError):self.send()
                next(iter(self.client.uploads.values()))[1][field]=value
                self.send();self.assertEqual(self.client.sent,[1,2,1,2,3,4])
    def test_missing_server_part_is_resent(self):
        self.client.fail_part=3
        with self.assertRaises(TimeoutError):self.send()
        del next(iter(self.client.uploads.values()))[1]
        self.send();self.assertEqual(self.client.sent,[1,2,3,1,3,4])
    def test_paginated_listing_resumes_ordered_parts(self):
        self.client.fail_part=4;self.client.page_size=1
        with self.assertRaises(TimeoutError):self.send()
        self.send();self.assertEqual(self.client.markers,[0,1,2]);self.assertEqual(self.client.sent,[1,2,3,4,4])
    def test_invalid_pagination_does_not_complete_or_discard_progress(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        with patch.object(self.client,'list_parts',return_value={'Parts':[],'IsTruncated':True,'NextPartNumberMarker':0}):
            with self.assertRaisesRegex(ValueError,'pagination'):self.send()
        self.assertEqual(self.state()['phase'],'active');self.assertEqual(self.client.completed,[])
    def test_expired_upload_starts_new_upload(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        self.client.uploads.clear();self.send()
        self.assertEqual(self.client.created,2);self.assertEqual(self.client.sent,[1,2,1,2,3,4])
    def test_lost_completion_response_uses_public_readback_without_resending(self):
        self.client.fail_completion=True
        with self.assertRaises(TimeoutError):self.send()
        self.assertEqual(self.state()['phase'],'active');sent=list(self.client.sent)
        self.send();self.assertEqual(self.client.sent,sent);self.assertEqual(self.client.created,1)
    def test_readback_timeout_retains_completion_and_retry_never_resends(self):
        with self.assertRaises(TimeoutError):self.send(lambda:(_ for _ in ()).throw(TimeoutError('fixture readback')))
        self.assertEqual(self.state()['phase'],'completed');sent=list(self.client.sent)
        self.send();self.assertEqual(self.client.sent,sent)
    def test_deleted_completed_object_is_uploaded_again(self):
        self.send();self.client.objects.clear();self.send()
        self.assertEqual(self.client.created,2)
    def test_public_digest_mismatch_invalidates_checkpoint_and_retains_body(self):
        with self.assertRaises(m.PublicReadbackMismatch):self.send(lambda:('0'*64,len(self.payload)))
        self.assertIsNone(self.state());self.assertTrue(self.body.exists())
        self.assertEqual(self.cache.execute('SELECT count(*) FROM _s3_upload_parts').fetchone()[0],0)
        self.send();self.assertEqual(self.client.created,2)
    def test_access_denied_does_not_reset_remote_upload(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        self.client.list_error='AccessDenied'
        with self.assertRaises(ClientError):self.send()
        self.assertEqual(self.client.created,1);self.assertEqual(self.state()['phase'],'active')
    def test_local_content_or_checkpoint_identity_change_is_rejected(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        self.body.write_bytes(b'X'+self.payload[1:])
        with self.assertRaisesRegex(ValueError,'SHA-256 changed'):self.send()
        self.body.write_bytes(self.payload)
        with self.assertRaisesRegex(ValueError,'checkpoint identity'):self.send(part_size=9)
        self.assertEqual(self.client.created,1);self.assertEqual(self.client.sent,[1,2])
    def test_body_changed_after_initial_hash_is_not_completed(self):
        import io
        original=Path.open;opened=[]
        def opening(path,*args,**kwargs):
            if path==self.body and args and args[0]=='rb':
                opened.append(1)
                if len(opened)==2:return io.BytesIO(b'X'+self.payload[1:])
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',new=opening):
            with self.assertRaisesRegex(ValueError,'body changed during transfer'):self.send()
        self.assertEqual(self.client.completed,[]);self.assertEqual(self.client.objects,{})
        self.send();self.assertEqual(self.client.sent,[1,2,3,4,1])
    def test_malformed_part_acknowledgement_is_not_checkpointed(self):
        self.client.bad_etag=True
        with self.assertRaisesRegex(ValueError,'ETag'):self.send()
        self.assertEqual(self.cache.execute('SELECT count(*) FROM _s3_upload_parts').fetchone()[0],0)
        self.send();self.assertEqual(self.client.sent,[1,1,2,3,4])
    def test_busy_object_claim_never_creates_an_upload(self):
        identity=('vn-archive','mirrors',self.key);claim=hashlib.sha256(('s3-upload\n'+'\n'.join(identity)).encode()).hexdigest()
        with exclusive_file(self.store.root/'.claims'/('upload-'+claim+'.lock')):
            with self.assertRaises(BlockingIOError):self.send()
        self.assertEqual(self.client.created,0)
    def test_invalid_parameters_and_open_transactions_do_not_start_network_work(self):
        with self.assertRaisesRegex(ValueError,'parameters'):self.send(part_size=0)
        self.store.db.execute('BEGIN')
        try:
            with self.assertRaisesRegex(ValueError,'committed'):self.send()
        finally:self.store.db.rollback()
        self.assertEqual(self.client.created,0)
    def test_upload_memory_is_bounded_by_one_part(self):
        size=20*1024**2;chunk=b'x'*(5*1024**2)
        with self.body.open('wb') as f:
            for _ in range(4):f.write(chunk)
        sha=a.digest(self.body)['sha256'];maximum=[]
        def send(**kw):maximum.append(len(kw['Body']));return {'ETag':'opaque-'+str(kw['PartNumber'])}
        # Mock.call_args_list would itself retain every body and measure the
        # test recorder rather than the uploader's live buffers.
        self.client.upload_part=send
        self.client.complete_multipart_upload=lambda **kwargs:{}
        tracemalloc.start()
        try:
            m.transfer_and_readback(self.store,self.client,'vn-archive','mirrors','files/'+sha,self.body,sha,size,lambda:(sha,size),part_size=5*1024**2)
            self.assertLess(tracemalloc.get_traced_memory()[1],8*1024**2)
        finally:tracemalloc.stop()
        self.assertEqual(maximum,[5*1024**2]*4)
    def test_network_work_never_holds_a_sqlite_snapshot_or_writer(self):
        writer=sqlite3.connect(m.cache_path(self.store.root),timeout=.1)
        original=self.client.upload_part
        def upload(**kwargs):
            self.assertFalse(self.store.db.in_transaction)
            writer.execute("UPDATE _s3_uploads SET created_at=created_at");writer.commit()
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
            return original(**kwargs)
        self.client.upload_part=upload
        try:self.send()
        finally:writer.close()
    def test_namespace_isolation_prevents_reusing_another_bucket_upload(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        other=Gateway()
        def proof():
            body=other.objects[self.key];return hashlib.sha256(body).hexdigest(),len(body)
        m.transfer_and_readback(self.store,other,'another-namespace','mirrors',self.key,self.body,self.sha,len(self.payload),proof,part_size=8)
        self.assertEqual(other.sent,[1,2,3,4])
        self.assertEqual(self.cache.execute('SELECT count(*) FROM _s3_uploads').fetchone()[0],2)
    def test_invalid_remote_part_number_cannot_be_used_for_completion(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        with patch.object(self.client,'list_parts',return_value={'Parts':[{'PartNumber':True,'Size':8,'ETag':'opaque'}]}):
            with self.assertRaisesRegex(ValueError,'listing'):self.send()
        self.assertEqual(self.client.completed,[])
    def test_legacy_unfiltered_metadata_backup_has_no_private_checkpoint(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        self.assertIsNotNone(self.state())
        snapshot=sqlite3.connect(':memory:')
        try:
            self.store.db.backup(snapshot)
            self.assertFalse(snapshot.execute("SELECT name FROM sqlite_master WHERE name IN ('_s3_uploads','_s3_upload_parts')").fetchall())
        finally:snapshot.close()
    def test_operational_tables_are_removed_only_from_public_snapshot(self):
        self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.send()
        # Also remove empty tables created by early draft initializers.
        m.initialize(self.store.db)
        snapshot=sqlite3.connect(':memory:')
        try:
            self.store.db.backup(snapshot);a._compact_snapshot(snapshot)
            self.assertFalse(snapshot.execute("SELECT name FROM sqlite_master WHERE name IN ('_s3_uploads','_s3_upload_parts')").fetchall())
            self.assertTrue(snapshot.execute("SELECT name FROM sqlite_master WHERE name='items'").fetchone())
        finally:snapshot.close()
        self.assertIsNotNone(self.state())


class PublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        cache=m.cache_path(self.store.root);cache.parent.mkdir(parents=True,exist_ok=True)
        self.cache=sqlite3.connect(cache);self.cache.row_factory=sqlite3.Row;m.initialize(self.cache);self.addCleanup(self.cache.close)
        self.record=a.parse_detail(HTML,'https://source.test/items/target','lathercraft')[0];self.record['id']='target';self.store.add(self.record)
        body=self.store.root/'files/body';body.parent.mkdir();body.write_bytes(PAYLOAD)
        self.sha=hashlib.sha256(PAYLOAD).hexdigest();self.key='files/'+self.sha
        self.store.update('target',status='verified',path='files/body',sha256=self.sha,md5=hashlib.md5(PAYLOAD).hexdigest(),size=len(PAYLOAD))
        self.client=Gateway()
    def proof(self,*args):
        b=self.client.objects[self.key];return hashlib.sha256(b).hexdigest(),len(b)
    def publish(self,proof=None):
        with patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',side_effect=proof or self.proof):
            return a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,resumable=True)
    def test_default_large_file_uses_durable_parts_and_full_readback(self):
        body=b'x'*(m.PART_SIZE+1)
        (self.store.root/'files/body').write_bytes(body)
        self.sha=hashlib.sha256(body).hexdigest();self.key='files/'+self.sha
        self.record['expected_md5']=hashlib.md5(body).hexdigest()
        self.record['expected_size']=len(body)
        self.store.add(self.record)
        self.store.update('target',status='verified',path='files/body',sha256=self.sha,md5=self.record['expected_md5'],size=len(body))
        with patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',side_effect=self.proof) as proof:
            self.assertEqual(a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False),1)
        self.assertEqual(self.client.sent,[1,2]);proof.assert_called_once()
        self.assertEqual(self.store.db.execute("SELECT remote_sha256 FROM items WHERE id='target'").fetchone()[0],self.sha)

    def test_default_small_file_keeps_single_request_and_full_readback(self):
        def upload(path,bucket,key,**kwargs):self.client.objects[key]=Path(path).read_bytes()
        with patch.object(self.client,'upload_file',side_effect=upload) as upload_file,patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',side_effect=self.proof) as proof:
            self.assertEqual(a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False),1)
        upload_file.assert_called_once();proof.assert_called_once();self.assertEqual(self.client.created,0)

    def test_success_records_proof_and_clears_only_transfer_checkpoint(self):
        self.assertEqual(self.publish(),1)
        row=self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
        self.assertEqual(row['remote_sha256'],self.sha);self.assertIsNotNone(row['remote_verified_at'])
        self.assertTrue((self.store.root/row['path']).exists())
        self.assertEqual(self.cache.execute('SELECT count(*) FROM _s3_uploads').fetchone()[0],0)
    def test_mismatch_never_creates_a_download_link_or_evicts_local_body(self):
        with self.assertRaises(m.PublicReadbackMismatch):self.publish(lambda *args:('0'*64,len(PAYLOAD)))
        row=self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
        self.assertIsNone(row['remote_verified_at']);self.assertTrue((self.store.root/row['path']).exists())
        self.assertIsNone(self.store.export()['files'][0]['download_url'])
    def test_source_cas_failure_preserves_completion_and_local_body(self):
        with patch.object(self.store,'update_if_current',return_value=False):
            with self.assertRaisesRegex(ValueError,'source metadata changed'):self.publish()
        self.assertEqual(self.cache.execute('SELECT phase FROM _s3_uploads').fetchone()[0],'completed')
        self.assertIsNone(self.store.db.execute("SELECT remote_verified_at FROM items WHERE id='target'").fetchone()[0])
        self.assertTrue((self.store.root/'files/body').exists());sent=list(self.client.sent)
        self.publish();self.assertEqual(self.client.sent,sent)
    def test_quarantined_source_is_never_uploaded(self):
        record=json.loads(self.store.db.execute("SELECT data FROM items WHERE id='target'").fetchone()[0])
        record['fields']['source_association_quarantined']=True
        self.store.db.execute("UPDATE items SET data=? WHERE id='target'",(json.dumps(record),));self.store.db.commit()
        self.assertEqual(self.publish(),0);self.assertEqual(self.client.created,0)
        self.assertIsNone(self.store.db.execute("SELECT remote_verified_at FROM items WHERE id='target'").fetchone()[0])
    def test_cli_pilot_requires_id_before_opening_store(self):
        with patch.object(a,'Store') as store,self.assertRaises(SystemExit) as raised:
            a.main(['upload','--resumable'])
        self.assertEqual(raised.exception.code,2);store.assert_not_called()
    def test_cli_missing_id_does_not_start_publication(self):
        with patch.object(a,'publish') as publish,self.assertRaises(SystemExit) as raised:
            a.main(['--root',str(self.store.root),'upload','--id','missing','--resumable','--files-only'])
        self.assertEqual(raised.exception.code,2);publish.assert_not_called()
    def test_cli_respects_existing_source_file_claim(self):
        claim=hashlib.sha256(b'target').hexdigest()+'.lock'
        with exclusive_file(self.store.root/'.claims'/claim),patch.object(a,'publish') as publish:
            self.assertEqual(a.main(['--root',str(self.store.root),'upload','--id','target','--resumable','--files-only']),1)
        publish.assert_not_called()
