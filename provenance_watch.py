"""Publish saved HTML independently of body-transfer catalogue checkpoints."""
import argparse
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
from archive_lock import exclusive_file


def publish_once(root):
    with exclusive_file(Path(root)/'.claims'/'provenance-pass.lock',blocking=False):
        store=a.Store(root)
        try:
            return a.publish_metadata(store,'vn-archive','mirrors')
        finally:
            store.db.close()


def watch(root,interval=60):
    with exclusive_file(Path(root)/'.claims'/'provenance-watch.lock'):
        while True:
            # A new interpreter loads transfer fixes on every pass. The parent
            # keeps only the supervisor claim; the child owns the pass claim.
            result=subprocess.run([sys.executable,'-u',str(Path(__file__).resolve()),
                                   '--root',str(root),'--once'],stdin=subprocess.DEVNULL)
            print('provenance pass exit',result.returncode,flush=True)
            time.sleep(interval)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if args.once:
        try:publish_once(args.root)
        except Exception as error:
            print('provenance checkpoint failed',type(error).__name__,str(error),flush=True)
            raise SystemExit(1)
    else:watch(args.root)
