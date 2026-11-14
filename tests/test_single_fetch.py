import hashlib
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import archiver as a
import runner_file_upload as worker
import runner_upload as jobs
import single_fetch as controller
from test_archiver import HTML, PAYLOAD
from test_runner_transfer_probe import Response
from test_runner_upload import Signer
from test_staged_archive import Gateway as BaseGateway

class Gateway(BaseGateway, Signer):pass

class SingleFetchTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://publisher.test/item','lathercraft')[0]
        self.url='https://source.test/demo.zip'
        self.record.update(id='target',mirrors=[self.url],expected_size=len(PAYLOAD))
        self.store.add(self.record);self.client=Gateway();self.origins=0;self.jobs=0;self.reads=0
        p=patch.object(jobs,'gh',return_value={'object':{'sha':'a'*40}});p.start();self.addCleanup(p.stop)
    def row(self):return self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
    def execute(self,state,spec,journal,**kwargs):
        self.jobs+=1
        def origin(*args,**kwargs):
            self.origins+=1;return Response(PAYLOAD)
        def put(request,**kwargs):
            q=parse_qs(urlsplit(request.full_url).query)
            result=self.client.upload_part(UploadId=q['uploadId'][0],PartNumber=int(q['partNumber'][0]),Body=request.data,ContentLength=len(request.data))
            response=Response();response.headers['ETag']=result['ETag'];return response
        result=worker.transfer(spec,source_open=origin,part_open=put)
        result['nonce']=state['nonce'];state.update(run_id=123,conclusion='success',dispatch_attempted_at=a.now())
        controller._save(self.store,state);return result
    def readback(self,key):
        self.reads+=1;body=self.client.objects[key]
        return dict(sha256=hashlib.sha256(body).hexdigest(),md5=hashlib.md5(body).hexdigest(),size=len(body))
    def run_file(self,execute=None):
        return controller.run(self.store,'target',self.url,len(PAYLOAD),client=self.client,execute=execute or self.execute,readback=self.readback)
    def test_one_origin_get_no_host_body_and_one_public_read(self):
        with patch.object(a,'download',side_effect=AssertionError('host body')):result=self.run_file()
        self.assertEqual(result['accepted_id'],'target');self.assertEqual((self.origins,self.jobs,self.reads),(1,1,1))
        self.assertEqual(self.row()['status'],'archived');self.assertIsNone(self.row()['path']);self.assertIsNotNone(self.row()['remote_verified_at'])
        self.assertEqual(list(self.client.objects),['files/'+hashlib.sha256(PAYLOAD).hexdigest()])
        self.run_file(Mock(side_effect=AssertionError('refetch')));self.assertEqual((self.origins,self.jobs,self.reads),(1,1,1))
    def test_timeout_reuses_exact_job_and_upload(self):
        seen=[]
        def timeout(state,spec,journal,**kwargs):
            seen.append((state['nonce'],state['upload_id'],spec));state.update(run_id=123,dispatch_attempted_at=a.now())
            controller._save(self.store,state);raise TimeoutError()
        with self.assertRaises(TimeoutError):self.run_file(timeout)
        def resume(state,spec,journal,**kwargs):
            self.assertEqual((state['nonce'],state['upload_id'],spec),seen[0]);return self.execute(state,spec,journal,**kwargs)
        self.run_file(resume);self.assertEqual((self.client.created,self.origins,self.client.aborts),(1,1,0))
    def test_copy_ack_loss_does_not_refetch(self):
        self.client.lose_copy_ack=True
        with self.assertRaises(TimeoutError):self.run_file()
        self.assertIsNone(self.row()['remote_verified_at']);self.run_file(Mock(side_effect=AssertionError('duplicate job')))
        self.assertEqual((self.jobs,self.origins,self.reads),(1,1,1))
    def test_failed_job_aborts_before_retry(self):
        def failed(state,*args,**kwargs):
            state['conclusion']='failure';controller._save(self.store,state);raise RuntimeError('failed')
        with self.assertRaises(RuntimeError):self.run_file(failed)
        self.assertEqual(self.client.aborts,1);self.assertEqual(controller._load(self.store,'target')['phase'],'aborted')
        self.run_file();self.assertEqual((self.client.created,self.origins),(2,1))
    def test_wrong_runner_identity_cannot_accept(self):
        with self.assertRaises(jobs.RunnerIdentityMismatch):self.run_file(Mock(side_effect=jobs.RunnerIdentityMismatch('changed')))
        self.assertEqual(self.client.aborts,1);self.assertIsNone(self.row()['remote_verified_at'])
    def test_public_hash_failure_retains_receipt(self):
        with patch.object(self,'readback',return_value=dict(sha256='0'*64,md5='0'*32,size=len(PAYLOAD))):
            with self.assertRaises(ValueError):self.run_file()
        self.assertIsNone(self.row()['remote_verified_at']);self.run_file(Mock(side_effect=AssertionError('duplicate job')))
        self.assertEqual(self.origins,1)
    def test_changed_metadata_keeps_old_build_history(self):
        def timeout(state,*args,**kwargs):
            state.update(dispatch_attempted_at=a.now(),run_id=123);controller._save(self.store,state);raise TimeoutError()
        with self.assertRaises(TimeoutError):self.run_file(timeout)
        self.store.add({**self.record,'title':'Different current build'});result=self.run_file()
        self.assertTrue(result['source_changed']);self.assertNotEqual(result['accepted_id'],'target')
        self.assertIsNone(self.row()['remote_verified_at']);self.assertEqual(self.origins,1)
    def test_unobserved_origin_rejected_before_dispatch(self):
        with self.assertRaises(ValueError):controller.run(self.store,'target','https://other.test/body',len(PAYLOAD),client=self.client,execute=self.execute)
        self.assertEqual((self.client.created,self.jobs,self.origins),(0,0,0))
    def test_changed_cached_spec_is_rejected_without_dispatch(self):
        def timeout(state,*args,**kwargs):
            state.update(dispatch_attempted_at=a.now(),run_id=123);controller._save(self.store,state);raise TimeoutError()
        with self.assertRaises(TimeoutError):self.run_file(timeout)
        state=controller._load(self.store,'target');state['spec']['source_url']='https://other.test/body'
        controller._save(self.store,state)
        with self.assertRaisesRegex(ValueError,'specification identity'):self.run_file(Mock(side_effect=AssertionError('dispatch')))
        self.assertEqual(self.origins,0);self.assertEqual(self.client.created,1)
    def test_abort_failure_preserves_job_identity(self):
        def failed(state,*args,**kwargs):
            state['conclusion']='failure';controller._save(self.store,state);raise RuntimeError('failed')
        with patch.object(self.client,'abort_multipart_upload',side_effect=TimeoutError('lost abort')):
            with self.assertRaises(TimeoutError):self.run_file(failed)
        state=controller._load(self.store,'target')
        self.assertEqual(state['phase'],'job');self.assertEqual(self.client.created,1)
        self.assertEqual(self.origins,0);self.assertIsNone(self.row()['remote_verified_at'])

    def test_equivalent_source_claim_blocks_before_any_upload(self):
        from archive_lock import exclusive_file
        alias={**self.record,'id':'alias'};self.store.add(alias)
        lock=self.store.root/'.claims'/(hashlib.sha256(b'alias').hexdigest()+'.lock')
        with exclusive_file(lock,blocking=False):
            with self.assertRaises(BlockingIOError):self.run_file()
        self.assertEqual((self.client.created,self.origins),(0,0))

    def test_resumable_alias_journal_blocks_new_duplicate(self):
        self.store.add({**self.record,'id':'alias'})
        def timeout(state,*args,**kwargs):
            state.update(run_id=123,dispatch_attempted_at=a.now());controller._save(self.store,state);raise TimeoutError()
        with self.assertRaises(TimeoutError):self.run_file(timeout)
        with self.assertRaises(BlockingIOError):
            controller.run(self.store,'alias',self.url,len(PAYLOAD),client=self.client,execute=self.execute)
        self.assertEqual((self.client.created,self.origins),(1,0))
    def test_batch_single_fetch_never_calls_host_download(self):
        import bulk
        with patch.object(jobs,'execute_job',side_effect=self.execute),patch.object(controller,'make_client',return_value=self.client),patch.object(controller.staged,'digest_hashes',side_effect=lambda *args,**kw:self.readback('files/'+hashlib.sha256(PAYLOAD).hexdigest())),patch.object(bulk,'download',side_effect=AssertionError('host download')),patch.object(bulk,'publish',side_effect=AssertionError('local upload')),patch.object(bulk,'publish_metadata'):
            stats=bulk.archive_batch(self.store,a.Fetcher(),single_fetch=True,reserve_bytes=0)
        self.assertEqual(stats['published'],1);self.assertEqual(self.origins,1);self.assertIsNone(self.row()['path'])

    def test_batch_timeout_never_falls_back_to_host_download(self):
        import bulk
        def timeout(state,*args,**kwargs):
            state.update(run_id=123,dispatch_attempted_at=a.now());controller._save(self.store,state);raise TimeoutError()
        with patch.object(jobs,'execute_job',side_effect=timeout),patch.object(controller,'make_client',return_value=self.client),patch.object(bulk,'download',side_effect=AssertionError('duplicate host download')),patch.object(bulk,'publish_metadata'):
            stats=bulk.archive_batch(self.store,a.Fetcher(),single_fetch=True,reserve_bytes=0)
        self.assertEqual(stats['busy'],1);self.assertEqual(stats['published'],0);self.assertEqual(self.client.created,1)

    def test_candidate_preserves_existing_host_partial(self):
        directory=self.store.root/'files'/self.row()['site']/'target';directory.mkdir(parents=True)
        (directory/'body.part').write_bytes(b'partial')
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute))
        self.assertEqual(self.client.created,0)

    def test_candidate_keeps_adapter_sources_on_host_route(self):
        self.store.add({**self.record,'mirrors':['https://drive.google.com/uc?id=fixture']})
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute))
        self.assertEqual(self.client.created,0)
