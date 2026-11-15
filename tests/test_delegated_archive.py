import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock,patch

import archiver as a
import delegated_archive as delegated
import single_fetch as controller
import test_single_fetch as fixtures
from archive_lock import exclusive_file


class DelegatedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SingleFetchTests('test_one_origin_get_no_host_body_and_one_public_read')
        self.f.setUp();self.addCleanup(self.f.doCleanups);self.store=self.f.store
        self.f.record.update(release_name='体験版',filename='demo.zip');self.store.add(self.f.record)
        self.fetcher=Mock()

    def add(self,ident,kind='trial'):
        self.store.add({**self.f.record,'id':ident,'title':ident,'filename':ident+'.zip','release_name':{'trial':'体験版','movie':'ムービー','other':'素材'}[kind]})
        with self.store.db:self.store.db.execute('UPDATE item_catalog SET kind=? WHERE item_id=?',(kind,ident))

    def test_priority_cursor_survives_reopen_and_wraps_without_starvation(self):
        self.add('aaa','movie');self.add('zzz','other')
        self.assertEqual(delegated.candidates(self.store,'lathercraft',1),[(0,'target')])
        delegated.advance(self.store,'lathercraft',0,'target')
        again=a.Store(self.store.root)
        try:self.assertEqual(delegated.candidates(again,'lathercraft',1),[(1,'aaa')])
        finally:again.db.close()
        delegated.advance(self.store,'lathercraft',2,'zzz')
        self.assertEqual(delegated.candidates(self.store,'lathercraft',1),[(0,'target')])

    def test_ineligible_first_row_does_not_starve_following_candidate(self):
        self.add('zzz')
        with patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',side_effect=[None,{'accepted_id':'zzz'}]) as run:
            first=delegated.archive_pass(self.store,self.fetcher,site='lathercraft',limit=1)
            second=delegated.archive_pass(self.store,self.fetcher,site='lathercraft',limit=1)
        self.assertEqual((first['ineligible'],second['published']),(1,1))
        self.assertEqual([call.args[1]['id'] for call in run.call_args_list],['target','zzz'])

    def test_busy_primary_claim_advances_cursor_without_new_job(self):
        self.add('zzz')
        claim=self.store.root/'.claims'/(hashlib.sha256(b'target').hexdigest()+'.lock')
        with exclusive_file(claim,blocking=False),patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',return_value=None) as run:
            stats=delegated.archive_pass(self.store,self.fetcher,site='lathercraft',limit=1)
        self.assertEqual(stats['busy'],1);self.assertEqual(run.call_args.args[1]['id'],'zzz')

    def test_managed_partial_and_quarantine_are_preserved(self):
        self.add('partial');self.add('quarantine')
        folder=self.store.root/'files/lathercraft/partial';folder.mkdir(parents=True);(folder/'file.part').write_bytes(b'keep')
        row=self.store.db.execute("SELECT data FROM items WHERE id='quarantine'").fetchone();record=json.loads(row[0]);record['fields']={**record.get('fields',{}),'source_association_quarantined':True}
        with self.store.db:self.store.db.execute("UPDATE items SET data=? WHERE id='quarantine'",(json.dumps(record),))
        with patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',return_value=None) as run:
            stats=delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        self.assertEqual(stats['ineligible'],3);self.assertEqual(run.call_count,1);self.assertEqual((folder/'file.part').read_bytes(),b'keep')

    def test_real_controller_full_proof_without_host_fallback_or_open_transaction(self):
        original=controller.archive_candidate
        def archive(store,row,**kwargs):
            self.assertFalse(store.db.in_transaction)
            return original(store,row,client=self.f.client,execute=self.f.execute,readback=self.f.readback)
        with patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',side_effect=archive),patch.object(a,'download',side_effect=AssertionError('host origin')):
            stats=delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        self.assertEqual(stats['published'],1);self.assertEqual((self.f.origins,self.f.jobs,self.f.reads),(1,1,1))
        self.assertEqual(self.f.row()['status'],'archived');self.assertIsNone(self.f.row()['path'])

    def test_timeout_keeps_dispatched_handle_for_independent_recovery(self):
        def execute(state,*args,**kwargs):
            state.update(run_id=123,dispatch_attempted_at=a.now());controller._save(self.store,state);raise TimeoutError()
        original=controller.archive_candidate
        with patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',side_effect=lambda store,row,**kwargs:original(store,row,client=self.f.client,execute=execute)):
            stats=delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        state=controller._load(self.store,'target');self.assertEqual((stats['busy'],state['run_id'],self.f.client.created,self.f.client.aborts),(1,123,1,0))

    def test_no_two_passes_mutate_same_site_cursor(self):
        with exclusive_file(delegated._claim(self.store,'lathercraft','pass'),blocking=False):
            with self.assertRaises(BlockingIOError):delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        self.assertIsNone(self.store.db.execute("SELECT name FROM sqlite_master WHERE name='delegated_scan_cursor'").fetchone())

    def test_terminal_runner_failure_preserves_host_route_and_prevents_repeat_jobs(self):
        original=controller.archive_candidate
        def failed(state,*args,**kwargs):
            state.update(run_id=123,conclusion='failure',dispatch_attempted_at=a.now())
            controller._save(self.store,state);raise RuntimeError('terminal')
        with patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',side_effect=lambda store,row,**kwargs:original(store,row,client=self.f.client,execute=failed)),patch.object(a,'download',side_effect=AssertionError('inline host fallback')):
            stats=delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        self.assertEqual((stats['failed'],stats['host_routes']),(1,1));self.assertEqual(self.f.row()['status'],'pending')
        state=controller._load(self.store,'target');self.assertEqual((state['phase'],state['run_id'],self.f.client.aborts),('aborted',123,1))
        attempts=self.store.db.execute("SELECT count(*) FROM attempts WHERE item_id='target'").fetchone()[0];self.assertGreater(attempts,0)
        with patch.object(controller,'archive_candidate',side_effect=AssertionError('repeat runner')):
            skipped=delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        self.assertEqual((skipped['ineligible'],skipped['attempted']),(1,0))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM attempts WHERE item_id='target'").fetchone()[0],attempts)
        self.store.add({**self.f.record,'release_name':'体験版 ver2'})
        with patch.object(delegated.bulk,'reuse_verified',return_value=False),patch.object(delegated.bulk,'equivalent_upload_active',return_value=False),patch.object(controller,'archive_candidate',return_value=None) as run:
            delegated.archive_pass(self.store,self.fetcher,site='lathercraft')
        self.assertEqual(run.call_count,1)

    def test_uncertain_job_cannot_be_requeued_as_a_host_route(self):
        delegated.candidates(self.store,'lathercraft',1)
        controller._prepare(self.store,dict(self.f.row()),self.f.url,len(fixtures.PAYLOAD),self.f.client)
        with self.store.db:self.store.db.execute("UPDATE items SET status='failed',error='All eligible observed mirrors failed in delegated transfer' WHERE id='target'")
        self.assertFalse(delegated.host_route(self.store,self.f.row()))
        self.assertEqual(self.f.row()['status'],'failed');self.assertEqual(controller._load(self.store,'target')['phase'],'job')
