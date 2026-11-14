import base64
import hashlib
import json
import itertools
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlencode,urlsplit,parse_qs
import zlib

import archiver as a
import multipart_upload as m
import runner_upload as r
import runner_file_upload as w
from test_archiver import HTML,PAYLOAD
from archive_lock import exclusive_file
from test_multipart_upload import Gateway as BaseGateway
from test_runner_transfer_probe import Response


class Signer:
    def generate_presigned_url(self,operation,Params,**kwargs):
        q=urlencode({'partNumber':Params['PartNumber'],'uploadId':Params['UploadId'],
                     'X-Amz-SignedHeaders':'content-length;host','X-Amz-Expires':1800,'X-Amz-Signature':'a'*64})
        return 'https://vn-archive.s3.hf.co/mirrors/'+Params['Key']+'?'+q


class Gateway(BaseGateway):
    def abort_multipart_upload(self,**kwargs):
        self.aborts+=1;self.uploads.pop(kwargs['UploadId'],None)


def specification(body=PAYLOAD,numbers=None,part_size=m.PART_SIZE):
    sha=hashlib.sha256(body).hexdigest();numbers=numbers or list(range(1,(len(body)+part_size-1)//part_size+1));parts=[]
    for n in numbers:
        url=Signer().generate_presigned_url('upload_part',Params={'PartNumber':n,'UploadId':'fixture','Key':'files/'+sha})
        parts.append({'number':n,'url':url})
    return {'sha256':sha,'size':len(body),'part_size':part_size,'parts':parts,'source_url':'https://mirror.test/体験版 file.zip','referer':'https://publisher.test/game/'}


class WorkerTests(unittest.TestCase):
    def test_selected_parts_do_not_rewrite_reused_prefix(self):
        body=b'x'*w.MIN_PART+b'last';spec=specification(body,[2],w.MIN_PART);calls=[]
        def send(request,**kwargs):calls.append(request);return Response()
        result=w.transfer(spec,source_open=lambda *args,**kwargs:Response(body),part_open=send)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].data,b'last')
        self.assertEqual(result['parts'][0]['number'],2);self.assertEqual(result['parts'][0]['sha256'],hashlib.sha256(b'last').hexdigest())
        self.assertFalse(result['completed_public_object'])

    def test_changed_original_cannot_send_any_part(self):
        send=Mock()
        with self.assertRaisesRegex(ValueError,'digest or size'):w.transfer(specification(),source_open=lambda *a,**k:Response(b'x'*len(PAYLOAD)),part_open=send)
        send.assert_not_called()

    def test_bounded_encoded_specification(self):
        value=specification();self.assertEqual(w.decode_spec(w.encode_spec(value)),value)
        oversized=base64.b64encode(zlib.compress(b'x'*(w.MAX_SPEC+1))).decode()
        with self.assertRaises(ValueError):w.decode_spec(oversized)

    def test_rejects_private_or_non_http_sources(self):
        for url in ['file:///etc/passwd','ftp://mirror.test/file','http://127.0.0.1/file','http://169.254.169.254/file','https://localhost/file','https://user:pass@mirror.test/file']:
            with self.subTest(url=url),self.assertRaises(ValueError):w.validate_spec(dict(specification(),source_url=url))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        self.payload=PAYLOAD
        self.body=self.store.root/'files/body';self.body.parent.mkdir();self.body.write_bytes(self.payload)
        self.sha=hashlib.sha256(PAYLOAD).hexdigest();self.key='files/'+self.sha
        record=a.parse_detail(HTML,'https://publisher.test/game/','lathercraft')[0];record['id']='target';self.store.add(record)
        self.store.update('target',status='verified',path='files/body',sha256=self.sha,md5=hashlib.md5(PAYLOAD).hexdigest(),size=len(PAYLOAD),source_url='https://mirror.test/demo.zip')
        self.client=Gateway();self.specs=[]
        self.addCleanup(patch.stopall)
        patch.object(r.PartClient,'_signer',return_value=Signer()).start()
        patch.object(r,'gh',return_value={'object':{'sha':'fixture-head'}}).start()
        patch.object(r.subprocess,'check_output',return_value='fixture-head').start()
        patch.object(a,'s3_client',return_value=self.client).start()
        patch.object(a,'public_digest',side_effect=self.readback).start()

    def readback(self,*args):
        body=self.client.objects[self.key];return hashlib.sha256(body).hexdigest(),len(body)

    def execute(self,adapter,state,spec,journal):
        self.specs.append(spec)
        def send(request,**kwargs):
            q=parse_qs(urlsplit(request.full_url).query)
            result=self.client.upload_part(Bucket='mirrors',Key=self.key,UploadId=q['uploadId'][0],
                PartNumber=int(q['partNumber'][0]),Body=request.data,ContentLength=len(request.data))
            return Response(etag=result['ETag'])
        receipt=w.transfer(spec,source_open=lambda *a,**k:Response(self.payload),part_open=send);receipt['nonce']=state['nonce'];return receipt

    def publish(self,**kwargs):
        with patch.object(r.PartClient,'_execute',autospec=True,side_effect=self.execute):
            return a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,via_runner=True,**kwargs)

    def test_real_transfer_helper_keeps_completion_readback_and_source_proof(self):
        self.assertEqual(self.publish(),1)
        row=self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
        self.assertEqual(row['remote_sha256'],self.sha);self.assertIsNotNone(row['remote_verified_at'])
        self.assertEqual(len(self.specs),1);self.assertEqual(self.client.sent,[1]);self.assertEqual(len(self.client.completed),1)
        self.assertTrue(self.body.exists())
        db=sqlite3.connect(m.cache_path(self.store.root));self.assertEqual(db.execute('SELECT count(*) FROM _s3_uploads').fetchone()[0],0);db.close()

    def test_resume_delegates_only_missing_parts_and_preserves_old_etag(self):
        self.payload=b'x'*m.PART_SIZE+b'last';self.body.write_bytes(self.payload)
        self.sha=hashlib.sha256(self.payload).hexdigest();self.key='files/'+self.sha
        record=json.loads(self.store.db.execute("SELECT data FROM items WHERE id='target'").fetchone()[0])
        record.update(expected_md5=hashlib.md5(self.payload).hexdigest(),expected_size=len(self.payload));self.store.add(record)
        self.store.update('target',status='verified',path='files/body',sha256=self.sha,md5=record['expected_md5'],size=len(self.payload),source_url='https://mirror.test/demo.zip')
        upload=self.client.create_multipart_upload(Bucket='mirrors',Key=self.key)['UploadId'];prefix=self.payload[:m.PART_SIZE]
        accepted=self.client.upload_part(Bucket='mirrors',Key=self.key,UploadId=upload,PartNumber=1,Body=prefix,ContentLength=len(prefix))
        path=m.cache_path(self.store.root);path.parent.mkdir(exist_ok=True);db=sqlite3.connect(path);m.initialize(db)
        db.execute('INSERT INTO _s3_uploads VALUES(?,?,?,?,?,?,?,?,?)',('vn-archive','mirrors',self.key,self.sha,len(self.payload),m.PART_SIZE,upload,'active','fixture'))
        db.execute('INSERT INTO _s3_upload_parts VALUES(?,?,?,?,?,?,?)',('vn-archive','mirrors',self.key,1,accepted['ETag'],len(prefix),hashlib.sha256(prefix).hexdigest()));db.commit();db.close()
        self.assertEqual(self.publish(),1)
        self.assertEqual([p['number'] for p in self.specs[0]['parts']],[2]);self.assertEqual(self.client.sent,[1,2])
        self.assertEqual(self.client.completed[0][0]['ETag'],accepted['ETag'])

    def test_lost_dispatch_ack_and_observation_failure_follow_same_run(self):
        adapter=r.PartClient(self.store,self.client,'vn-archive','mirrors','hf',self.body,self.sha,len(PAYLOAD),'https://mirror.test/demo.zip',signer=Signer())
        state={'nonce':'fixture','head_sha':'fixture-head'};spec=specification();journal=self.store.root/'job.json';calls=[];observations=[0]
        receipt={'nonce':'fixture','sha256':self.sha}
        def api(arguments):
            calls.append(arguments)
            if '/dispatches' in arguments[1]:raise RuntimeError('fixture acknowledgement lost')
            if '/runs?' in arguments[1]:
                observations[0]+=1
                if observations[0]==1:raise RuntimeError('fixture poll failed')
                return {'workflow_runs':[{'id':123,'display_title':'Runner upload fixture'}]}
            if arguments[:2]==['run','download']:
                folder=Path(arguments[arguments.index('--dir')+1]);(folder/'runner-upload.json').write_text(json.dumps(receipt));return None
            return {'status':'completed','conclusion':'success','head_sha':'fixture-head'}
        with patch.object(r,'gh',side_effect=api),patch.object(r.time,'sleep'),patch.object(r.time,'monotonic',side_effect=itertools.count()):
            self.assertEqual(adapter._execute(state,spec,journal),receipt)
        self.assertEqual(sum('/dispatches' in call[1] for call in calls),1);self.assertEqual(state['run_id'],123)

    def test_changed_source_receipt_never_completes_or_marks_source(self):
        def bad(adapter,state,spec,journal):
            receipt=self.execute(adapter,state,spec,journal);receipt['sha256']='0'*64;return receipt
        with patch.object(r.PartClient,'_execute',autospec=True,side_effect=bad),self.assertRaisesRegex(ValueError,'identity'):
            a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,via_runner=True)
        self.assertEqual(self.client.aborts,1);self.assertFalse(self.client.completed);self.assertTrue(self.body.exists())
        self.assertIsNone(self.store.db.execute("SELECT remote_verified_at FROM items WHERE id='target'").fetchone()[0])

    def test_failed_job_invalidates_capabilities_before_local_resume(self):
        with patch.object(r.PartClient,'_execute',side_effect=TimeoutError('fixture observer deadline')),self.assertRaises(TimeoutError):
            a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,via_runner=True)
        self.assertEqual(self.client.aborts,1);self.assertTrue(self.body.exists())
        self.assertEqual(a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,resumable=True),1)
        self.assertEqual(self.client.created,2)

    def test_source_cas_failure_retains_verified_object_and_never_redispatches(self):
        with patch.object(self.store,'update_if_current',return_value=False),self.assertRaisesRegex(ValueError,'source metadata changed'):self.publish()
        self.assertTrue(self.body.exists());sent=list(self.client.sent)
        self.assertEqual(a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,resumable=True),1)
        self.assertEqual(self.client.sent,sent);self.assertEqual(len(self.specs),1)

    def test_lost_completion_ack_does_not_redownload_or_resend_source(self):
        self.client.fail_completion=True
        with self.assertRaises(TimeoutError):self.publish()
        sent=list(self.client.sent)
        self.assertEqual(a.publish(self.store,'vn-archive','mirrors',ident='target',metadata=False,resumable=True),1)
        self.assertEqual(self.client.sent,sent);self.assertEqual(len(self.specs),1)

    def test_local_client_cannot_write_while_delegation_may_still_run(self):
        upload=self.client.create_multipart_upload(Bucket='mirrors',Key=self.key)['UploadId']
        path=m.cache_path(self.store.root);path.parent.mkdir(exist_ok=True)
        db=sqlite3.connect(path);m.initialize(db)
        db.execute('INSERT INTO _s3_uploads VALUES(?,?,?,?,?,?,?,?,?)',('vn-archive','mirrors',self.key,self.sha,len(PAYLOAD),m.PART_SIZE,upload,'delegated','fixture'));db.commit();db.close()
        with self.assertRaises(BlockingIOError):m.transfer_and_readback(self.store,self.client,'vn-archive','mirrors',self.key,self.body,self.sha,len(PAYLOAD),lambda:self.readback())
        self.assertFalse(self.client.sent);self.assertFalse(self.client.completed)

    def test_busy_runner_cli_does_not_generate_full_json(self):
        claim=self.store.root/'.claims'/(hashlib.sha256(b'target').hexdigest()+'.lock')
        with exclusive_file(claim),patch.object(a.Store,'export',side_effect=AssertionError('runner must remain SQLite-only')) as export:
            self.assertEqual(a.main(['--root',str(self.store.root),'upload','--id','target','--runner-upload','--files-only']),1)
        export.assert_not_called();self.assertTrue(self.body.exists())

    def test_runner_cli_does_not_print_capabilities_from_exceptions(self):
        with patch.object(a,'publish',side_effect=RuntimeError('sensitive-capability')),patch('sys.stderr',new_callable=io.StringIO) as output,patch.object(a.Store,'export') as export:
            self.assertEqual(a.main(['--root',str(self.store.root),'upload','--id','target','--runner-upload','--files-only']),1)
        self.assertNotIn('sensitive-capability',output.getvalue());export.assert_not_called()

    def test_cli_requires_one_id_and_files_only_before_opening_store(self):
        for args in [['upload','--runner-upload'],['upload','--runner-upload','--id','target']]:
            with self.subTest(args=args),patch.object(a,'Store') as store,self.assertRaises(SystemExit):a.main(args)
            store.assert_not_called()
