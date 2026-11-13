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


if __name__=='__main__':unittest.main()
