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
from urllib.error import HTTPError
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
    def test_default_fetcher_uses_public_dropbox_download_adapter(self):
        spec=specification();original='https://www.dropbox.com/s/public-id/patch.zip?dl=0&rlkey=a%2Bb'
        spec['source_url']=original;fetcher=Mock();fetcher.open.return_value=Response(PAYLOAD)
        with patch.object(a,'Fetcher',return_value=fetcher):
            result=w.transfer(spec,part_open=lambda *args,**kwargs:Response())
        fetcher.open.assert_called_once_with('https://www.dropbox.com/s/public-id/patch.zip?rlkey=a%2Bb&dl=1',{'Referer':spec['referer']})
        self.assertEqual(spec['source_url'],original)
        self.assertEqual(result['sha256'],hashlib.sha256(PAYLOAD).hexdigest())

    def test_default_download_adapter_still_rejects_changed_body_before_upload(self):
        spec=specification();spec['source_url']='https://www.dropbox.com/s/public-id/patch.zip?dl=0'
        fetcher=Mock();fetcher.open.return_value=Response(b'x'*len(PAYLOAD));send=Mock()
        with patch.object(a,'Fetcher',return_value=fetcher),self.assertRaisesRegex(ValueError,'digest or size'):
            w.transfer(spec,part_open=send)
        send.assert_not_called()

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

    def test_source_http_failure_logs_status_without_sending_or_leaking_url(self):
        error=HTTPError('https://source.test/private?token=secret',403,'secret-message',{},None)
        send=Mock()
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            with self.assertRaises(HTTPError) as caught:
                w.transfer(specification(),source_open=Mock(side_effect=error),part_open=send)
        self.assertIs(caught.exception,error);self.assertTrue(error.closed);send.assert_not_called()
        text=output.getvalue()
        self.assertIn('"phase": "source"',text);self.assertIn('"status": 403',text)
        for private in ['secret','source.test','token=']:self.assertNotIn(private,text)

    def test_part_http_failure_logs_status_and_never_returns_success(self):
        error=HTTPError('https://vn-archive.s3.hf.co/private?signature=secret',503,'secret-message',{},None)
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            with self.assertRaises(HTTPError) as caught:
                w.transfer(specification(),source_open=lambda *a,**k:Response(PAYLOAD),part_open=Mock(side_effect=error))
        self.assertIs(caught.exception,error);self.assertTrue(error.closed);text=output.getvalue()
        self.assertIn('runner source verified',text)
        self.assertIn('"phase": "upload-part"',text);self.assertIn('"status": 503',text)
        for private in ['secret','s3.hf.co','signature=']:self.assertNotIn(private,text)

    def test_http_failure_rejects_non_numeric_status_in_log(self):
        error=HTTPError('https://source.test/private','secret-code','secret-message',{},None)
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            with self.assertRaises(HTTPError):
                w.transfer(specification(),source_open=Mock(side_effect=error))
        self.assertTrue(error.closed);self.assertIn('"status": null',output.getvalue());self.assertNotIn('secret',output.getvalue())

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


class FiveGBWorkerBoundsTests(unittest.TestCase):
    def specification(self,size):
        key='staging/'+'a'*64;part_size=m.PART_SIZE
        parts=[dict(number=n,url=Signer().generate_presigned_url('upload_part',Params={'PartNumber':n,'UploadId':'fixture','Key':key})) for n in range(1,(size+part_size-1)//part_size+1)]
        return dict(mode='staging',staging_key=key,sha256=None,md5=None,published_size=None,size=size,part_size=part_size,parts=parts,source_url='https://publisher.test/trial.zip')

    def test_above_four_gib_and_five_gb_boundary_use_bounded_part_descriptions(self):
        self.assertEqual(w.MAX_BYTES,5_000_000_000)
        for size in (2*1024**3+1,4*1024**3+1,w.MAX_BYTES):
            with self.subTest(size=size):
                spec=self.specification(size);w.validate_spec(spec)
                self.assertEqual(w.decode_spec(w.encode_spec(spec)),spec)
        self.assertEqual(len(self.specification(w.MAX_BYTES)['parts']),597)

    def test_oversized_body_and_invalid_part_size_are_rejected_before_origin(self):
        for change in [dict(size=w.MAX_BYTES+1),dict(part_size=64*1024**2+1),dict(part_size=w.MIN_PART-1)]:
            source=Mock();send=Mock()
            with self.subTest(change=change),self.assertRaises(ValueError):
                w.transfer(dict(self.specification(w.MAX_BYTES),**change),source_open=source,part_open=send)
            source.assert_not_called();send.assert_not_called()

    def test_realistic_signed_five_gb_spec_fits_dispatch_and_decoded_limits(self):
        import boto3
        from botocore.config import Config
        signer=boto3.client('s3',endpoint_url='https://vn-archive.s3.hf.co',aws_access_key_id='synthetic-access-key',aws_secret_access_key='synthetic-secret-key',config=Config(signature_version='s3v4',region_name='us-east-1',s3={'addressing_style':'path'}))
        self.addCleanup(signer.close);spec=self.specification(w.MAX_BYTES)
        for part in spec['parts']:
            part['url']=signer.generate_presigned_url('upload_part',Params={'Bucket':'mirrors','Key':spec['staging_key'],'UploadId':'synthetic-upload-'+('z'*160),'PartNumber':part['number'],'ContentLength':m.PART_SIZE},ExpiresIn=1800,HttpMethod='PUT')
        w.validate_spec(spec);encoded=w.encode_spec(spec)
        self.assertLessEqual(len(encoded),60000);self.assertLessEqual(len(json.dumps(spec,separators=(',',':')).encode()),w.MAX_SPEC)
        self.assertEqual(w.decode_spec(encoded),spec)


class RetainedWorkerReleaseTests(unittest.TestCase):
    def test_all_retained_worker_releases_keep_saved_run_and_commit_identity(self):
        self.assertEqual(r.RUNNER_REF,'archive-runner-v4')
        for ref in ['archive-runner','archive-runner-v2','archive-runner-v3',r.RUNNER_REF]:
            with self.subTest(ref=ref),tempfile.TemporaryDirectory() as tmp:
                state=dict(nonce='retained',head_sha='a'*40,dispatch_ref=ref,run_id=123,dispatch_attempted_at='2026-11-14T00:00:00+00:00')
                calls=[]
                def api(arguments):
                    calls.append(arguments)
                    if arguments[:2]==['run','download']:
                        (Path(arguments[arguments.index('--dir')+1])/'runner-upload.json').write_text('{}');return None
                    return dict(status='completed',conclusion='success',head_sha='a'*40)
                with patch.object(r,'gh',side_effect=api):
                    self.assertEqual(r.execute_job(state,specification(),Path(tmp)/'job.json'),{})
                self.assertEqual(state['dispatch_ref'],ref);self.assertEqual(state['head_sha'],'a'*40)
                self.assertFalse(any('/dispatches' in call[1] for call in calls))

    def test_saved_legacy_release_is_observed_without_redispatch_or_head_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Path(tmp)/'job.json';state=dict(nonce='legacy',head_sha='a'*40,dispatch_ref='archive-runner',run_id=123,dispatch_attempted_at='2026-11-14T00:00:00+00:00');calls=[]
            def api(arguments):
                calls.append(arguments)
                if arguments[:2]==['run','download']:
                    (Path(arguments[arguments.index('--dir')+1])/'runner-upload.json').write_text('{}');return None
                return dict(status='completed',conclusion='success',head_sha='a'*40)
            with patch.object(r,'gh',side_effect=api):self.assertEqual(r.execute_job(state,specification(),journal),{})
            self.assertFalse(any('/dispatches' in call[1] for call in calls));self.assertEqual(state['head_sha'],'a'*40);self.assertEqual(state['dispatch_ref'],'archive-runner')

    def test_legacy_release_still_requires_the_original_commit_identity(self):
        state=dict(nonce='legacy',head_sha='a'*40,dispatch_ref='archive-runner',run_id=123,dispatch_attempted_at='2026-11-14T00:00:00+00:00')
        with tempfile.TemporaryDirectory() as tmp,patch.object(r,'gh',return_value=dict(status='completed',conclusion='success',head_sha='b'*40)):
            with self.assertRaises(r.RunnerIdentityMismatch):r.execute_job(state,specification(),Path(tmp)/'job.json')


class FourGiBReleaseRetentionTests(unittest.TestCase):
    def test_saved_v2_release_resumes_without_dispatch_or_head_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=Path(tmp)/'job.json';state=dict(nonce='legacy-v2',head_sha='a'*40,dispatch_ref='archive-runner-v2',run_id=123,dispatch_attempted_at='2026-11-14T00:00:00+00:00');calls=[]
            def api(arguments):
                calls.append(arguments)
                if arguments[:2]==['run','download']:
                    (Path(arguments[arguments.index('--dir')+1])/'runner-upload.json').write_text('{}');return None
                return dict(status='completed',conclusion='success',head_sha='a'*40)
            with patch.object(r,'gh',side_effect=api):self.assertEqual(r.execute_job(state,specification(),journal),{})
            self.assertFalse(any('/dispatches' in call[1] for call in calls));self.assertEqual(state['head_sha'],'a'*40);self.assertEqual(state['dispatch_ref'],'archive-runner-v2')

    def test_saved_v2_release_rejects_a_different_head(self):
        state=dict(nonce='legacy-v2',head_sha='a'*40,dispatch_ref='archive-runner-v2',run_id=123,dispatch_attempted_at='2026-11-14T00:00:00+00:00')
        with tempfile.TemporaryDirectory() as tmp,patch.object(r,'gh',return_value=dict(status='completed',conclusion='success',head_sha='b'*40)):
            with self.assertRaises(r.RunnerIdentityMismatch):r.execute_job(state,specification(),Path(tmp)/'job.json')
