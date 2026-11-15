from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import archive_watch as watch


class FreshArchiveWatchTests(unittest.TestCase):
    def test_empty_normal_queue_closes_store_without_downloading(self):
        store=Mock()
        with patch.object(watch.a,'Store',return_value=store),patch.object(watch,'ready',return_value=0),patch.object(watch,'archive_batch') as batch:
            watch.once('archive','hanny')
        store.db.close.assert_called_once();batch.assert_not_called()

    def test_normal_pass_closes_store_after_transfer_failure(self):
        store=Mock()
        with patch.object(watch.a,'Store',return_value=store),patch.object(watch,'ready',return_value=1),patch.object(watch,'archive_batch',side_effect=OSError('synthetic transfer failure')):
            with self.assertRaises(OSError):watch.once('archive','hanny',single_fetch=True)
        store.db.close.assert_called_once()

    def test_fresh_child_is_finite_preserves_modes_and_releases_watch_lock(self):
        stop=Mock();stop.is_set.return_value=False;stop.wait.return_value=True
        with tempfile.TemporaryDirectory() as root,patch.object(watch.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            watch.watch_fresh(root,'hanny',stop,runner_upload=True,single_fetch=True)
            command=run.call_args.args[0]
            self.assertIn('--once',command);self.assertNotIn('--fresh-processes',command)
            self.assertIn('--runner-upload',command);self.assertIn('--single-fetch',command)
            self.assertEqual(command[command.index('--site')+1],'hanny')
            stop.wait.assert_called_once_with(60)
            from archive_lock import exclusive_file
            with exclusive_file(Path(root)/'.claims/watch-hanny.lock',blocking=False):pass

    def test_failed_finite_pass_uses_longer_cooldown(self):
        stop=Mock();stop.is_set.return_value=False;stop.wait.return_value=True
        with tempfile.TemporaryDirectory() as root,patch.object(watch.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            watch.watch_fresh(root,'hanny',stop,interval=60)
        stop.wait.assert_called_once_with(300)

    def test_launch_failure_retries_after_cooldown_without_abandoning_watch(self):
        stop=Mock();stop.is_set.return_value=False;stop.wait.side_effect=[False,True]
        with tempfile.TemporaryDirectory() as root,patch.object(watch.subprocess,'run',side_effect=[OSError('synthetic launch failure'),SimpleNamespace(returncode=0)]) as run:
            watch.watch_fresh(root,'hanny',stop)
        self.assertEqual(run.call_count,2)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list],[300,60])
