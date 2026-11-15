import hashlib
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError,URLError
import ssl
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
    def test_lost_dispatch_can_be_found_beyond_first_hundred_runs(self):
        first=[dict(id=i,display_title='unrelated') for i in range(100)]
        with patch.object(jobs,'gh',side_effect=[{'workflow_runs':first},{'workflow_runs':[dict(id=999,display_title='Runner upload target')]}]) as api:
            self.assertEqual(jobs.find_run({'nonce':'target'}),999)
        self.assertIn('page=2',api.call_args.args[0][1])

    def test_duplicate_nonce_is_not_silently_assigned_to_a_run(self):
        with patch.object(jobs,'gh',return_value={'workflow_runs':[dict(id=i,display_title='Runner upload target') for i in [1,2]]}):
            with self.assertRaises(jobs.RunnerIdentityMismatch):jobs.find_run({'nonce':'target'})
    def test_terminal_failure_is_recorded_and_tries_next_observed_mirror(self):
        self.store.add({**self.record,'mirrors':[self.url,'https://fallback.test/demo.zip']})
        seen=[]
        def execute(state,spec,journal,**kwargs):
            seen.append(spec['source_url'])
            if len(seen)==1:
                state.update(run_id=123,conclusion='failure');controller._save(self.store,state);raise RuntimeError('source unavailable')
            return self.execute(state,spec,journal,**kwargs)
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=execute,readback=self.readback)
        self.assertEqual(result['accepted_id'],'target');self.assertEqual(seen,[self.url,'https://fallback.test/demo.zip'])
        self.assertEqual(self.client.aborts,1);self.assertEqual(self.origins,1)
        attempts=self.store.db.execute("SELECT url,error FROM attempts WHERE item_id='target'").fetchall()
        self.assertEqual(len(attempts),1);self.assertEqual(attempts[0]['url'],self.url)
        self.assertIn('failure',attempts[0]['error'])

    def test_exhausted_mirrors_stop_without_retrying_same_source_in_one_pass(self):
        self.store.add({**self.record,'mirrors':[self.url,'https://fallback.test/demo.zip']});seen=[]
        def failed(state,spec,journal,**kwargs):
            seen.append(spec['source_url']);state.update(run_id=123,conclusion='failure');controller._save(self.store,state);raise RuntimeError('source failure')
        with self.assertRaises(controller.SourceTransferFailed):
            controller.archive_candidate(self.store,self.row(),client=self.client,execute=failed)
        self.assertEqual(seen,[self.url,'https://fallback.test/demo.zip']);self.assertEqual(self.client.aborts,2)
        self.assertEqual(self.row()['status'],'failed');self.assertIsNone(self.row()['remote_verified_at'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM attempts WHERE item_id='target'").fetchone()[0],2)

    def test_abort_failure_does_not_start_fallback(self):
        self.store.add({**self.record,'mirrors':[self.url,'https://fallback.test/demo.zip']});seen=[]
        def failed(state,spec,journal,**kwargs):
            seen.append(spec['source_url']);state.update(run_id=123,conclusion='failure');controller._save(self.store,state);raise RuntimeError('failed')
        with patch.object(self.client,'abort_multipart_upload',side_effect=TimeoutError('abort uncertain')):
            with self.assertRaises(TimeoutError):controller.archive_candidate(self.store,self.row(),client=self.client,execute=failed)
        self.assertEqual(seen,[self.url]);self.assertEqual(self.client.created,1)
        self.assertEqual(controller._load(self.store,'target')['phase'],'job')
    def test_batch_continues_other_files_after_exhausted_source(self):
        import bulk
        self.store.add({**self.record,'id':'zz-second'})
        def execute(state,spec,journal,**kwargs):
            if state['item_id']=='target':
                state.update(run_id=123,conclusion='failure');controller._save(self.store,state);raise RuntimeError('source failure')
            return self.execute(state,spec,journal,**kwargs)
        with patch.object(jobs,'execute_job',side_effect=execute),patch.object(controller,'make_client',return_value=self.client),patch.object(controller.staged,'digest_hashes',side_effect=lambda *args,**kwargs:self.readback('files/'+hashlib.sha256(PAYLOAD).hexdigest())),patch.object(bulk,'download',side_effect=AssertionError('unexpected host download')),patch.object(bulk,'publish_metadata'):
            stats=bulk.archive_batch(self.store,a.Fetcher(),single_fetch=True,reserve_bytes=0)
        self.assertEqual(stats['failed'],1);self.assertEqual(stats['published'],1)
        self.assertEqual(self.row()['status'],'failed');self.assertEqual(self.origins,1)
    def test_unknown_publisher_size_uses_head_without_reading_host_body(self):
        self.store.add({**self.record,'expected_size':None})
        response=Response();response.headers['Content-Length']=str(len(PAYLOAD))
        response.read=Mock(side_effect=AssertionError('HEAD body read'))
        probe=Mock(return_value=response)
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,readback=self.readback,probe=probe)
        self.assertEqual(result['accepted_id'],'target');probe.assert_called_once();response.read.assert_not_called()
        self.assertEqual((self.origins,self.jobs),(1,1));self.assertIsNone(self.row()['path'])
        state=controller._load(self.store,'target');self.assertIsNone(state['spec']['published_size'])
        self.assertEqual(state['size'],len(PAYLOAD))

    def test_range_metadata_resolves_refused_wayback_head_without_changing_source_transport(self):
        capture='https://web.archive.org/web/20150326205535id_/http://source.test/demo.zip'
        self.store.add({**self.record,'site':'getchu','expected_size':None,'expected_md5':None,'mirrors':[capture]})
        with self.store.db:self.store.db.execute('INSERT INTO wayback_candidates VALUES(?,?,?,?,?)',('target',capture,self.url,'20150326205535','A'*32))
        response=Response(status=206);response.headers.update({'Content-Range':f'bytes 0-0/{len(PAYLOAD)}','Content-Length':'1','Content-Type':'application/zip'})
        response.read=Mock(side_effect=AssertionError('metadata probe read body'))
        head=Mock(side_effect=URLError(ConnectionRefusedError('refused')));ranged=Mock(return_value=response)
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,readback=self.readback,probe=head,range_probe=ranged)
        self.assertEqual(result['accepted_id'],'target');self.assertTrue(response.closed);response.read.assert_not_called()
        self.assertEqual(ranged.call_args.kwargs['headers']['Range'],'bytes=0-0')
        state=controller._load(self.store,'target');self.assertEqual(state['spec']['source_url'],capture)
        self.assertIsNone(state['spec']['published_size']);self.assertIsNone(state['spec']['md5'])
        self.assertEqual((self.origins,self.jobs,self.reads),(1,1,1))

    def test_unconfirmed_replay_does_not_add_range_request_after_connection_refusal(self):
        capture='https://web.archive.org/web/20150326205535id_/http://source.test/demo.zip'
        self.store.add({**self.record,'expected_size':None,'mirrors':[capture]})
        ranged=Mock(side_effect=AssertionError('unconfirmed replay triggered extra request'))
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,
            probe=Mock(side_effect=URLError(ConnectionRefusedError('refused'))),range_probe=ranged)
        self.assertIsNone(result);ranged.assert_not_called();self.assertEqual(self.client.created,0)

    def test_head_method_denial_can_use_closed_range_ignored_size_response(self):
        self.store.add({**self.record,'expected_size':None})
        response=Response();response.headers['Content-Length']=str(len(PAYLOAD));response.read=Mock(side_effect=AssertionError('metadata body'))
        error=HTTPError(self.url,405,'unsupported',{},None)
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,readback=self.readback,probe=Mock(side_effect=error),range_probe=Mock(return_value=response))
        self.assertEqual(result['accepted_id'],'target');self.assertTrue(error.closed);self.assertTrue(response.closed);response.read.assert_not_called()

    def test_invalid_range_metadata_does_not_dispatch_or_read_body(self):
        self.store.add({**self.record,'expected_size':None})
        for headers in [{},{'Content-Range':'bytes 1-1/17'},{'Content-Range':'bytes 0-0/*'},{'Content-Range':f'bytes 0-0/{worker.MAX_BYTES+1}'},{'Content-Range':'bytes 0-0/17','Content-Type':'text/html'},{'Content-Range':'bytes 0-0/17','Content-Encoding':'gzip'}]:
            with self.subTest(headers=headers):
                head=Response();response=Response(status=206);response.headers.update(headers);response.read=Mock(side_effect=AssertionError('metadata body'))
                self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=Mock(return_value=head),range_probe=Mock(return_value=response)))
                self.assertTrue(response.closed);response.read.assert_not_called()
        self.assertEqual(self.client.created,0)

    def test_range_metadata_does_not_retry_http_denials_or_certificate_failures(self):
        self.store.add({**self.record,'expected_size':None})
        errors=[HTTPError(self.url,code,'denied',{},None) for code in [403,404,429,503]]
        errors += [URLError(ssl.SSLCertVerificationError('certificate')),TimeoutError('slow'),URLError(ConnectionRefusedError('native source refused'))]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                ranged=Mock(side_effect=AssertionError('unexpected extra probe'))
                self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=Mock(side_effect=error),range_probe=ranged))
                ranged.assert_not_called()
                if isinstance(error,HTTPError):self.assertTrue(error.closed)

    def test_optional_range_metadata_keeps_three_candidate_budget(self):
        self.store.add({**self.record,'expected_size':None,'mirrors':['https://source.test/'+str(i)+'.zip' for i in range(7)]})
        def unsupported(*args,**kwargs):raise HTTPError(self.url,405,'unsupported',{},None)
        head=Mock(side_effect=unsupported);ranged=Mock(side_effect=lambda *a,**k:Response(status=206))
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=head,range_probe=ranged))
        self.assertEqual(head.call_count,3);self.assertEqual(ranged.call_count,3);self.assertEqual(self.client.created,0)

    def test_invalid_or_unsupported_head_keeps_normal_host_route(self):
        self.store.add({**self.record,'expected_size':None})
        for headers in [{},{'Content-Length':'rounded 10MB'},{'Content-Length':'0'},{'Content-Length':str(worker.MAX_BYTES+1)},{'Content-Length':'17','Content-Encoding':'gzip'},{'Content-Length':'17','Content-Type':'text/html'}]:
            with self.subTest(headers=headers):
                response=Response();response.headers.update(headers)
                response.read=Mock(side_effect=AssertionError('HEAD body'))
                self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=Mock(return_value=response)))
                response.read.assert_not_called()
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=Mock(side_effect=OSError('HEAD unsupported'))))
        self.assertEqual(self.client.created,0)

    def test_fetcher_head_keeps_url_encoding_tls_opener_and_identity_headers(self):
        fetcher=a.Fetcher(delay=0,retries=1);opener=Mock();fetcher.opener=opener
        fetcher.head('https://source.test/日本語 file.zip',headers={'Referer':'https://publisher.test/'})
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'HEAD');self.assertNotIn('日本語',request.full_url)
        self.assertIn('%20',request.full_url);self.assertEqual(request.get_header('Accept-encoding'),'identity')
        self.assertEqual(request.get_header('Referer'),'https://publisher.test/')
    def test_observed_dated_binary_capture_can_use_single_fetch(self):
        capture='https://web.archive.org/web/20181025223313id_/http://source.test/demo.zip'
        with self.store.db:
            self.store.db.execute('INSERT INTO wayback_candidates VALUES(?,?,?,?,?)',('target',capture,self.url,'20181025223313',None))
        result=controller.run(self.store,'target',capture,len(PAYLOAD),client=self.client,execute=self.execute,readback=self.readback)
        self.assertEqual(result['accepted_id'],'target');self.assertEqual(self.row()['source_url'],capture)
        self.assertEqual(self.origins,1)

    def test_unknown_size_head_probes_are_bounded_before_host_fallback(self):
        self.store.add({**self.record,'expected_size':None,'mirrors':['https://source.test/'+str(i)+'.zip' for i in range(7)]})
        probe=Mock(side_effect=OSError('HEAD refused'))
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=probe))
        self.assertEqual(probe.call_count,3);self.assertEqual(self.client.created,0)
    def test_new_jobs_pin_the_worker_branch_instead_of_main(self):
        self.run_file()
        state=controller._load(self.store,'target')
        self.assertEqual(state['dispatch_ref'],jobs.RUNNER_REF)
        jobs.gh.assert_any_call(['api','repos/'+jobs.REPO+'/git/ref/heads/'+jobs.RUNNER_REF])

    def test_dispatch_request_preserves_the_pinned_reference(self):
        from test_runner_upload import specification
        state=dict(nonce='pinned-fixture',head_sha='a'*40,dispatch_ref=jobs.RUNNER_REF)
        journal=self.store.root/'pinned-job.json';seen=[]
        def api(args):
            if '/dispatches' in args[1]:
                import json
                from pathlib import Path
                seen.append(json.loads(Path(args[args.index('--input')+1]).read_text()));return None
            if '/runs?' in args[1]:return {'workflow_runs':[dict(id=123,display_title='Runner upload pinned-fixture')]}
            if args[:2]==['run','download']:
                from pathlib import Path
                (Path(args[args.index('--dir')+1])/'runner-upload.json').write_text('{}');return None
            return dict(status='completed',conclusion='success',head_sha='a'*40)
        with patch.object(jobs,'gh',side_effect=api):jobs.execute_job(state,specification(),journal)
        self.assertEqual(seen[0]['ref'],jobs.RUNNER_REF);self.assertEqual(state['run_id'],123)

    def test_unconfigured_dispatch_reference_is_rejected_before_intent(self):
        state=dict(nonce='pinned-fixture',head_sha='a'*40,dispatch_ref='unconfigured')
        with self.assertRaisesRegex(ValueError,'unconfigured'):
            jobs.execute_job(state,{},self.store.root/'job.json')
        self.assertNotIn('dispatch_attempted_at',state)
    def test_identity_abort_retries_same_healthy_source_instead_of_rotating(self):
        self.store.add({**self.record,'mirrors':[self.url,'https://fallback.test/demo.zip']})
        with self.assertRaises(jobs.RunnerIdentityMismatch):
            self.run_file(Mock(side_effect=jobs.RunnerIdentityMismatch('changed')))
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,readback=self.readback)
        self.assertEqual(result['accepted_id'],'target');self.assertEqual(self.row()['source_url'],self.url)
        self.assertEqual(self.client.aborts,1)


class HealthyObservedProbeTests(unittest.TestCase):
    setUp=SingleFetchTests.setUp
    row=SingleFetchTests.row
    execute=SingleFetchTests.execute
    readback=SingleFetchTests.readback
    def healthy_candidate(self,*,stale=False,listed=True,storage=False):
        from datetime import datetime,timedelta,timezone
        healthy='http://suezou.dyndns.org/trial.zip'
        archives=['https://web.archive.org/web/20181025223313id_/http://suezou.dyndns.org/trial.zip']
        archives+=['https://web.archive.org/web/20181025223313id_/http://dead.test/'+str(n)+'.zip' for n in range(2)]
        mirrors=archives+([healthy] if listed else [])
        if storage:mirrors.append('https://storage1.lathercraft.net/trial.zip')
        self.store.add({**self.record,'expected_size':None,'mirrors':mirrors})
        self.store.db.execute('CREATE TABLE source_health(site TEXT PRIMARY KEY,checked_at TEXT,healthy INTEGER,data TEXT)')
        import json
        stamp=(datetime.now(timezone.utc)-timedelta(hours=4 if stale else 0)).isoformat()
        self.store.db.execute('INSERT INTO source_health VALUES(?,?,?,?)',('suezou',stamp,1,json.dumps({'url':'http://suezou.dyndns.org/'})));self.store.db.commit()
        return healthy

    def test_fresh_healthy_observed_mirror_precedes_dead_archives_within_probe_budget(self):
        healthy=self.healthy_candidate();seen=[]
        def probe(url,headers=None):
            seen.append(url)
            if url!=healthy:raise OSError('dead archived mirror')
            response=Response(PAYLOAD);response.headers['Content-Length']=str(len(PAYLOAD));return response
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,readback=self.readback,probe=probe)
        self.assertEqual(result['accepted_id'],'target');self.assertEqual(seen,[healthy]);self.assertEqual(self.row()['source_url'],healthy)
        self.assertEqual((self.origins,self.jobs,self.reads),(1,1,1))

    def test_stale_health_does_not_displace_archived_candidates_or_expand_probe_budget(self):
        healthy=self.healthy_candidate(stale=True);probe=Mock(side_effect=OSError('HEAD unavailable'))
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=probe))
        self.assertEqual(probe.call_count,3);self.assertNotIn(healthy,[c.args[0] for c in probe.call_args_list]);self.assertEqual(self.client.created,0)

    def test_healthy_source_does_not_authorize_unlisted_live_urls(self):
        healthy=self.healthy_candidate(listed=False);probe=Mock(side_effect=OSError('HEAD unavailable'))
        self.assertIsNone(controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,probe=probe))
        self.assertEqual(probe.call_count,3);self.assertNotIn(healthy,controller._observed_urls(self.store,self.row(),self.record))
        with self.assertRaisesRegex(ValueError,'not observed'):
            controller.run(self.store,'target',healthy,len(PAYLOAD),client=self.client,execute=self.execute,readback=self.readback)
        self.assertEqual(self.client.created,0)

    def test_primary_storage_preference_is_preserved(self):
        self.healthy_candidate(storage=True);probe=Mock(return_value=Response(PAYLOAD));probe.return_value.headers['Content-Length']=str(len(PAYLOAD))
        result=controller.archive_candidate(self.store,self.row(),client=self.client,execute=self.execute,readback=self.readback,probe=probe)
        self.assertEqual(result['accepted_id'],'target');self.assertEqual(probe.call_args.args[0],'https://storage1.lathercraft.net/trial.zip')
