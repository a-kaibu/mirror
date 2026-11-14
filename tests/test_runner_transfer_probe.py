import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode
from unittest.mock import patch,Mock
import runner_transfer_probe as p


def spec(body=b'diagnostic',part_size=p.MIN_PART):
    sha=hashlib.sha256(body).hexdigest();urls=[]
    for number in range(1,(len(body)+part_size-1)//part_size+1):
        query=urlencode({'partNumber':number,'uploadId':'synthetic-upload','X-Amz-SignedHeaders':'content-length;host','X-Amz-Expires':900,'X-Amz-Signature':'a'*64})
        urls.append('https://vn-archive.s3.hf.co/mirrors/files/'+sha+'?'+query)
    return {'sha256':sha,'size':len(body),'part_size':part_size,'source_url':'https://huggingface.co/buckets/vn-archive/mirrors/resolve/files/'+sha,'part_urls':urls}


class Response(io.BytesIO):
    def __init__(self,body=b'',status=200,etag='opaque-etag'):
        super().__init__(body);self.status=status;self.headers={'ETag':etag} if etag else {}


class TransferProbeTests(unittest.TestCase):
    def test_uploads_only_verified_bytes_and_never_completes_object(self):
        body=b'x'*p.MIN_PART+b'end';data=spec(body);calls=[]
        def opened(request,**kwargs):
            calls.append(request)
            return Response(body) if request.get_method()=='GET' else Response()
        result=p.run_probe(data,opened)
        self.assertEqual([x.get_method() for x in calls],['GET','PUT','PUT'])
        self.assertEqual([len(x.data) for x in calls[1:]],[p.MIN_PART,3])
        self.assertEqual([x['size'] for x in result['parts']],[p.MIN_PART,3])
        self.assertFalse(result['completed_public_object'])
        self.assertEqual(result['sha256'],hashlib.sha256(body).hexdigest())
        for request in calls[1:]:self.assertEqual(request.get_header('Content-length'),str(len(request.data)))

    def test_mismatched_source_cannot_upload_any_part(self):
        calls=[]
        def opened(request,**kwargs):calls.append(request);return Response(b'wrong')
        with self.assertRaises(ValueError):p.run_probe(spec(),opened)
        self.assertEqual(len(calls),1)

    def test_oversized_response_is_rejected_before_upload(self):
        calls=[]
        def opened(request,**kwargs):calls.append(request);return Response(b'x'*100)
        with self.assertRaisesRegex(ValueError,'exceeded'):p.run_probe(spec(),opened)
        self.assertEqual(len(calls),1)

    def test_missing_acknowledgement_is_not_success(self):
        body=b'diagnostic'
        def opened(request,**kwargs):return Response(body) if request.get_method()=='GET' else Response(etag=None)
        with self.assertRaisesRegex(ValueError,'ETag'):p.run_probe(spec(body),opened)

    def test_rejects_unsafe_or_unbounded_capabilities(self):
        data=spec()
        variants=[]
        for old,new in [('vn-archive.s3.hf.co','evil.test'),('https://','http://'),('partNumber=1','partNumber=2'),('content-length%3Bhost','host'),('X-Amz-Expires=900','X-Amz-Expires=3600')]:
            changed=copy.deepcopy(data);changed['part_urls'][0]=changed['part_urls'][0].replace(old,new);variants.append(changed)
        changed=copy.deepcopy(data);changed['source_url']='http://127.0.0.1/private';variants.append(changed)
        changed=copy.deepcopy(data);changed['size']=p.MAX_BYTES+1;variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed),self.assertRaises(ValueError):p.validate_spec(changed)

    def test_all_parts_must_belong_to_same_upload(self):
        data=spec(b'x'*(p.MIN_PART+1));data['part_urls'][1]=data['part_urls'][1].replace('synthetic-upload','another-upload')
        with self.assertRaisesRegex(ValueError,'mixed'):p.validate_spec(data)


class ReceiptTests(unittest.TestCase):
    def test_accepts_only_server_confirmed_parts(self):
        from transfer_probe import verify_receipt
        state={'nonce':'fixture','sha256':'a'*64,'size':17}
        receipt={'nonce':'fixture','sha256':'a'*64,'size':17,'upload_seconds':1.5,'completed_public_object':False,
                 'parts':[{'number':1,'size':17,'etag':'opaque'}]}
        server=[{'PartNumber':1,'Size':17,'ETag':'opaque'}]
        self.assertEqual(verify_receipt(state,receipt,server),receipt)
        with self.assertRaisesRegex(ValueError,'acknowledgement'):verify_receipt(state,receipt,[{'PartNumber':1,'Size':17,'ETag':'other'}])
        with self.assertRaisesRegex(ValueError,'identity'):verify_receipt(state,dict(receipt,nonce='another'),server)
        with self.assertRaisesRegex(ValueError,'count'):verify_receipt(state,receipt,[])


class ControllerTests(unittest.TestCase):
    def failed_run(self,root,resume=False):
        import transfer_probe as t
        data=spec();sha=data['sha256'];client=Mock()
        client.head_object.return_value={'ContentLength':data['size'],'ETag':'original'}
        client.create_multipart_upload.return_value={'UploadId':'synthetic-upload'}
        client.generate_presigned_url.return_value=data['part_urls'][0]
        calls=[]
        def api(arguments):
            calls.append(arguments);endpoint=arguments[1] if arguments[0]=='api' else ''
            if endpoint.endswith('/git/ref/heads/main'):return {'object':{'sha':'fixture-head'}}
            if '/dispatches' in endpoint:return None
            if '/runs?' in endpoint:return {'workflow_runs':[{'display_title':'Transfer probe fixture-nonce','head_sha':'fixture-head','id':123}]}
            if endpoint.endswith('/runs/123'):return {'status':'completed','conclusion':'failure'}
            raise AssertionError('unexpected API call')
        if resume:
            t.save(Path(root)/'.work'/'transfer-probe.json',{'id':'fixture','sha256':sha,'size':data['size'],'key':'files/'+sha,
                'upload_id':'synthetic-upload','nonce':'fixture-nonce','head_sha':'fixture-head','original_etag':'original',
                'dispatch_attempted_at':'fixture-time','cleanup_complete':False})
        with patch.object(t,'client',return_value=client),patch.object(t,'select_object',return_value={'id':'fixture','sha256':sha,'size':data['size']}),patch.object(t,'gh',side_effect=api),patch.object(t.subprocess,'check_output',return_value='fixture-head'),patch.object(t.uuid,'uuid4',return_value=SimpleNamespace(hex='fixture-nonce')):
            with self.assertRaisesRegex(RuntimeError,'workflow failed'):t.run(root,resume=resume)
        client.abort_multipart_upload.assert_called_once();client.complete_multipart_upload.assert_not_called()
        self.assertTrue(json.loads((Path(root)/'.work'/'transfer-probe.json').read_text())['cleanup_complete'])
        return client,calls

    def test_failed_job_always_aborts_own_temporary_upload(self):
        with tempfile.TemporaryDirectory() as root:self.failed_run(root)

    def test_resume_follows_original_job_without_redispatch_or_new_upload(self):
        with tempfile.TemporaryDirectory() as root:
            client,calls=self.failed_run(root,resume=True)
        client.create_multipart_upload.assert_not_called()
        self.assertFalse(any('/dispatches' in ' '.join(call) for call in calls))

    def test_runner_error_log_does_not_disclose_capability(self):
        with tempfile.TemporaryDirectory() as folder:
            event=Path(folder)/'event.json';event.write_text(json.dumps({'inputs':{'spec':'{}','nonce':'fixture'}}))
            with patch('sys.argv',['runner_transfer_probe.py','--event',str(event),'--output',str(Path(folder)/'result.json')]),patch.object(p,'run_probe',side_effect=RuntimeError('sensitive-capability-value')),patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(p.main(),1)
            self.assertNotIn('sensitive-capability-value',output.getvalue())
