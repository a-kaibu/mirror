import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from archive_lock import exclusive_file
import delegated_sources as coordinator


class DelegatedSourcesTests(unittest.TestCase):
    def test_finite_passes_visit_every_source_with_at_most_two_children(self):
        gate=threading.Event();lock=threading.Lock();active=peak=0;commands=[]
        def run(command,**kwargs):
            nonlocal active,peak
            site=command[command.index('--site')+1]
            with lock:
                active+=1;peak=max(peak,active);commands.append(command)
                if active==2:gate.set()
            if site in ('first','second'):self.assertTrue(gate.wait(5))
            time.sleep(.01)
            with lock:active-=1
            return SimpleNamespace(returncode=0)
        results=coordinator.one_round(['first','second','third','first'],limit=2,scan_limit=17,workers=2,run=run)
        self.assertEqual([r['site'] for r in results],['first','second','third']);self.assertEqual(peak,2)
        self.assertTrue(all('--watch' not in c for c in commands))
        self.assertTrue(all(c[c.index('--limit')+1]=='2' and c[c.index('--scan-limit')+1]=='17' for c in commands))

    def test_launch_failure_does_not_hide_later_sources_or_leak_exception_text(self):
        def run(command,**kwargs):
            site=command[command.index('--site')+1]
            if site=='first':raise OSError('sensitive-capability')
            return SimpleNamespace(returncode=3 if site=='second' else 0)
        results=coordinator.one_round(['first','second','third'],run=run)
        self.assertEqual([r['exit_code'] for r in results],[None,3,0]);self.assertEqual(results[0]['error_type'],'OSError')
        self.assertNotIn('sensitive-capability',json.dumps(results))

    def test_invalid_scope_or_bounds_fail_before_starting_children(self):
        for sites,kwargs in [([],{}),(['ok','../bad'],{}),(['ok'],{'workers':0}),(['ok'],{'workers':3}),(['ok'],{'limit':0}),(['ok'],{'scan_limit':0})]:
            run=Mock()
            with self.subTest(sites=sites,kwargs=kwargs),self.assertRaises(ValueError):coordinator.one_round(sites,run=run,**kwargs)
            run.assert_not_called()

    def test_duplicate_coordinator_cannot_start_another_round(self):
        with tempfile.TemporaryDirectory() as root:
            with exclusive_file(Path(root)/'.claims/delegated-sources.lock',blocking=False),patch.object(coordinator,'one_round') as visit:
                with self.assertRaises(BlockingIOError):coordinator.main(['--root',root,'--site','first'])
                visit.assert_not_called()

    def test_watch_round_finishes_before_its_cooldown(self):
        with tempfile.TemporaryDirectory() as root,patch.object(coordinator,'one_round',return_value=[dict(site='first',exit_code=0)]) as visit,patch.object(coordinator.time,'sleep',side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):coordinator.main(['--root',root,'--site','first','--watch','--interval','120'])
            self.assertEqual(visit.call_count,1);sleep.assert_called_once_with(120)
