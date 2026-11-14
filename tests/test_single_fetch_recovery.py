import unittest
from unittest.mock import Mock,patch
import single_fetch as controller
import single_fetch_recovery as recovery
import test_single_fetch as fixtures
import archiver as a
from archive_lock import exclusive_file
import hashlib


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SingleFetchTests('test_one_origin_get_no_host_body_and_one_public_read')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.store=self.f.store

    def test_accepted_cleanup_after_metadata_change_never_starts_an_origin(self):
        with patch.object(self.f.client,'delete_object',side_effect=OSError('cleanup pending')):
            result=self.f.run_file()
        self.assertTrue(result['cleanup_pending'])
        self.store.add({**self.f.record,'title':'Changed observation'})
        with patch.object(controller,'_prepare',side_effect=AssertionError('new origin preparation')):
            result=recovery.recover_one(self.store,'target',client=self.f.client,execute=Mock(side_effect=AssertionError('dispatch')))
        self.assertFalse(result['cleanup_pending']);self.assertEqual(controller._load(self.store,'target')['phase'],'cleaned')
        self.assertEqual(self.f.origins,1)

    def test_staged_copy_ack_loss_resumes_without_another_job(self):
        self.f.client.lose_copy_ack=True
        with self.assertRaises(TimeoutError):self.f.run_file()
        result=recovery.recover_one(self.store,'target',client=self.f.client,execute=Mock(side_effect=AssertionError('new job')),readback=self.f.readback)
        self.assertEqual(result['accepted_id'],'target');self.assertEqual((self.f.origins,self.f.jobs),(1,1))
        self.assertEqual(recovery.pending(self.store),[])

    def test_active_source_claim_is_not_taken_over(self):
        controller._prepare(self.store,dict(self.f.row()),self.f.url,len(fixtures.PAYLOAD),self.f.client)
        claim=self.store.root/'.claims'/(hashlib.sha256(b'target').hexdigest()+'.lock')
        with exclusive_file(claim,blocking=False),patch.object(controller,'make_client',side_effect=AssertionError('active source')):
            result=recovery.recover_pass(self.store)
        self.assertEqual(result['busy'],1);self.assertEqual(controller._load(self.store,'target')['phase'],'job')

    def test_undispatched_spec_is_aborted_without_origin_or_new_upload(self):
        controller._prepare(self.store,dict(self.f.row()),self.f.url,len(fixtures.PAYLOAD),self.f.client)
        result=recovery.recover_one(self.store,'target',client=self.f.client,execute=Mock(side_effect=AssertionError('dispatch')))
        self.assertTrue(result['aborted_unused']);self.assertEqual((self.f.client.created,self.f.client.aborts,self.f.origins),(1,1,0))
        self.assertEqual(recovery.pending(self.store),[])

    def test_observation_timeout_preserves_the_exact_dispatched_handle(self):
        def timeout(state,*args,**kwargs):
            state.update(run_id=123,dispatch_attempted_at=a.now());controller._save(self.store,state);raise TimeoutError()
        with self.assertRaises(TimeoutError):self.f.run_file(timeout)
        before=controller._load(self.store,'target')
        with self.assertRaises(TimeoutError):
            recovery.recover_one(self.store,'target',client=self.f.client,execute=timeout,timeout=1)
        after=controller._load(self.store,'target')
        self.assertEqual((after['nonce'],after['upload_id'],after['run_id']),(before['nonce'],before['upload_id'],123))
        self.assertEqual((self.f.client.created,self.f.client.aborts,self.f.origins),(1,0,0))

    def test_uncertain_unused_upload_abort_keeps_its_journal(self):
        controller._prepare(self.store,dict(self.f.row()),self.f.url,len(fixtures.PAYLOAD),self.f.client)
        with patch.object(self.f.client,'abort_multipart_upload',side_effect=TimeoutError('uncertain')):
            with self.assertRaises(TimeoutError):recovery.recover_one(self.store,'target',client=self.f.client)
        self.assertEqual(controller._load(self.store,'target')['phase'],'job');self.assertEqual(self.f.origins,0)

    def test_journal_at_another_identity_path_is_not_used(self):
        controller._prepare(self.store,dict(self.f.row()),self.f.url,len(fixtures.PAYLOAD),self.f.client)
        path=controller.staged.journal_path(self.store,'target');path.rename(path.with_name('wrong.json'))
        with self.assertRaisesRegex(ValueError,'path identity'):recovery.pending(self.store)
