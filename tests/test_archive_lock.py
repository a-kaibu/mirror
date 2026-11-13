import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from archive_lock import exclusive_file

class ArchiveLockTests(unittest.TestCase):
    def test_checkpoint_lock_serializes_independent_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            child_code="from archive_lock import exclusive_file; from pathlib import Path; import sys; print('ready',flush=True); " + "\nwith exclusive_file(Path(sys.argv[1])/'lock'):\n print('acquired',flush=True)\n"
            with exclusive_file(Path(tmp)/"lock"):
                child=subprocess.Popen([sys.executable,"-u","-c",child_code,tmp],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(),"ready")
                    with self.assertRaises(subprocess.TimeoutExpired):child.wait(timeout=.2)
                except BaseException:
                    child.kill();child.communicate();raise
            output,error=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,error)
            self.assertIn("acquired",output)

    def test_exception_releases_checkpoint_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                with exclusive_file(Path(tmp)/"lock"):raise RuntimeError("test")
            with exclusive_file(Path(tmp)/"lock"):pass

    def test_nonblocking_claim_returns_busy_without_releasing_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"lock"
            code="""from archive_lock import exclusive_file
import sys
try:
    with exclusive_file(sys.argv[1],blocking=False):print('acquired')
except BlockingIOError:print('busy')
"""
            with exclusive_file(path):
                for _ in range(2):
                    child=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=5)
                    self.assertEqual(child.returncode,0,child.stderr)
                    self.assertEqual(child.stdout.strip(),'busy')
            child=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=5)
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertEqual(child.stdout.strip(),'acquired')
