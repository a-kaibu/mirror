import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import provenance_watch as p
from archive_lock import exclusive_file


class ProvenanceWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_once_closes_store_after_success_and_failure_and_releases_claim(self):
        store=Mock()
        with patch.object(p.a,'Store',return_value=store),patch.object(p.a,'publish_metadata',side_effect=[TimeoutError('interrupted public body'),True]) as publish:
            with self.assertRaises(TimeoutError):p.publish_once(self.root)
            self.assertTrue(p.publish_once(self.root))
        self.assertEqual(store.db.close.call_count,2)
        self.assertEqual(publish.call_count,2)
    def test_busy_pass_does_not_start_another_upload(self):
        with exclusive_file(self.root/'.claims/provenance-pass.lock',blocking=False),patch.object(p.a,'Store') as store:
            with self.assertRaises(BlockingIOError):p.publish_once(self.root)
        store.assert_not_called()
    def test_watch_reloads_child_after_failed_and_successful_passes(self):
        with patch.object(p.subprocess,'run',side_effect=[subprocess.CompletedProcess([],1),subprocess.CompletedProcess([],0),KeyboardInterrupt]) as run,patch.object(p.time,'sleep') as sleep:
            with self.assertRaises(KeyboardInterrupt):p.watch(self.root)
        self.assertEqual(run.call_count,3)
        for call in run.call_args_list:
            self.assertEqual(call.args[0],[sys.executable,'-u',str(Path(p.__file__).resolve()),'--root',str(self.root),'--once'])
            self.assertEqual(call.kwargs,{'stdin':subprocess.DEVNULL})
        self.assertEqual(sleep.call_args_list,[unittest.mock.call(60),unittest.mock.call(60)])
    def test_supervisor_claim_does_not_deadlock_its_publication_child(self):
        store=Mock();calls=0
        def child(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls>1:raise KeyboardInterrupt
            p.publish_once(self.root)
            return subprocess.CompletedProcess([],0)
        with patch.object(p.a,'Store',return_value=store),patch.object(p.a,'publish_metadata') as publish,patch.object(p.subprocess,'run',side_effect=child),patch.object(p.time,'sleep'):
            with self.assertRaises(KeyboardInterrupt):p.watch(self.root)
        publish.assert_called_once_with(store,'vn-archive','mirrors')
        store.db.close.assert_called_once()


    def test_catalogue_only_does_not_wait_for_provenance_pass(self):
        store=Mock()
        with exclusive_file(self.root/'.claims/provenance-pass.lock'),patch.object(p.a,'Store',return_value=store),patch.object(p.a,'publish_metadata',return_value=True) as publish:
            self.assertTrue(p.publish_once(self.root,catalogue_only=True))
        publish.assert_called_once_with(store,'vn-archive','mirrors',include_html=False,blocking=False)
        store.db.close.assert_called_once()

    def test_busy_catalogue_pass_does_not_create_another_store(self):
        with exclusive_file(self.root/'.claims/catalogue-pass.lock'),patch.object(p.a,'Store') as store:
            with self.assertRaises(BlockingIOError):p.publish_once(self.root,catalogue_only=True)
        store.assert_not_called()

    def test_catalogue_supervisor_reloads_mode_and_obeys_interval(self):
        with patch.object(p.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0),KeyboardInterrupt]) as run,patch.object(p.time,'sleep') as sleep:
            with self.assertRaises(KeyboardInterrupt):p.watch(self.root,1800,catalogue_only=True)
        self.assertTrue(all(call.args[0][-1]=='--catalogue-only' for call in run.call_args_list))
        sleep.assert_called_once_with(1800)


if __name__=='__main__':unittest.main()


class PagesPublicationWatchTests(unittest.TestCase):
    def test_pages_publication_has_a_separate_pass_and_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Mock();client=Mock()
            with exclusive_file(root/'.claims/catalogue-pass.lock'),patch.object(p.a,'Store',return_value=store),patch.object(p.a,'s3_client',return_value=client),patch('snapshot_upload.publish') as publish,patch.object(p.a,'publish_metadata') as full,patch.object(p,'request_pages_build') as trigger:
                self.assertTrue(p.publish_once(root,pages_only=True))
            publish.assert_called_once_with(store,client,'vn-archive','mirrors',compressed=True,pages=True);full.assert_not_called();store.db.close.assert_called_once();trigger.assert_called_once_with()

    def test_busy_pages_pass_and_conflicting_modes_never_open_a_store(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.a,'Store') as store:
            with exclusive_file(Path(tmp)/'.claims/pages-pass.lock'):
                with self.assertRaises(BlockingIOError):p.publish_once(tmp,pages_only=True)
            with self.assertRaises(ValueError):p.publish_once(tmp,pages_only=True,catalogue_only=True)
            store.assert_not_called()

    def test_pages_supervisor_reloads_a_bounded_child(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0),KeyboardInterrupt]) as run,patch.object(p.time,'sleep') as sleep:
            with self.assertRaises(KeyboardInterrupt):p.watch(tmp,300,pages_only=True)
            self.assertTrue(all(call.args[0][-1]=='--pages-only' for call in run.call_args_list));sleep.assert_called_once_with(300)


class PagesBuildTriggerTests(unittest.TestCase):
    def test_build_is_requested_only_after_verified_snapshot_publication(self):
        events=[]
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.a,'Store'),patch.object(p.a,'s3_client'),patch('snapshot_upload.publish',side_effect=lambda *args,**kwargs:events.append('verified')),patch.object(p,'request_pages_build',side_effect=lambda:events.append('build')):
            p.publish_once(tmp,pages_only=True)
        self.assertEqual(events,['verified','build'])

    def test_failed_snapshot_never_requests_a_build(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.a,'Store'),patch.object(p.a,'s3_client'),patch('snapshot_upload.publish',side_effect=TimeoutError()),patch.object(p,'request_pages_build') as trigger:
            with self.assertRaises(TimeoutError):p.publish_once(tmp,pages_only=True)
            trigger.assert_not_called()

    def test_trigger_failure_preserves_the_verified_snapshot_and_redacts_error(self):
        import io
        with tempfile.TemporaryDirectory() as tmp,patch.object(p.a,'Store'),patch.object(p.a,'s3_client'),patch('snapshot_upload.publish'),patch.object(p,'request_pages_build',side_effect=RuntimeError('sensitive diagnostic')),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertTrue(p.publish_once(tmp,pages_only=True))
            self.assertNotIn('sensitive diagnostic',output.getvalue());self.assertIn('RuntimeError',output.getvalue())

    def test_trigger_requests_the_existing_pages_workflow_on_main(self):
        with patch('transfer_probe.gh') as gh:p.request_pages_build()
        self.assertIn('repos/a-kaibu/mirror/actions/workflows/pages.yml/dispatches',gh.call_args.args[0]);self.assertEqual(gh.call_args.args[0][-1],'ref=main')
