"""Publish saved HTML independently of body-transfer catalogue checkpoints."""
import argparse
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
from archive_lock import exclusive_file


def publish_once(root,*,catalogue_only=False,pages_only=False):
    if catalogue_only and pages_only:raise ValueError('conflicting publication modes')
    claim='pages-pass.lock' if pages_only else 'catalogue-pass.lock' if catalogue_only else 'provenance-pass.lock'
    with exclusive_file(Path(root)/'.claims'/claim,blocking=False):
        store=a.Store(root)
        try:
            if pages_only:
                import snapshot_upload
                snapshot_upload.publish(store,a.s3_client('vn-archive'),'vn-archive','mirrors',compressed=True,pages=True)
                return True
            if catalogue_only:
                print('catalogue checkpoint starts',a.now(),flush=True)
                return a.publish_metadata(store,'vn-archive','mirrors',include_html=False,blocking=False)
            return a.publish_metadata(store,'vn-archive','mirrors')
        finally:
            store.db.close()


def watch(root,interval=60,*,catalogue_only=False,pages_only=False):
    if catalogue_only and pages_only:raise ValueError('conflicting publication modes')
    claim='pages-watch.lock' if pages_only else 'catalogue-watch.lock' if catalogue_only else 'provenance-watch.lock'
    with exclusive_file(Path(root)/'.claims'/claim):
        while True:
            # A new interpreter loads transfer fixes on every pass. The parent
            # keeps only the supervisor claim; the child owns the pass claim.
            command=[sys.executable,'-u',str(Path(__file__).resolve()),'--root',str(root),'--once']
            if catalogue_only:command.append('--catalogue-only')
            if pages_only:command.append('--pages-only')
            result=subprocess.run(command,stdin=subprocess.DEVNULL)
            print('pages pass exit' if pages_only else ('catalogue pass exit' if catalogue_only else 'provenance pass exit'),result.returncode,flush=True)
            time.sleep(interval)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive')
    parser.add_argument('--once',action='store_true')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--catalogue-only',action='store_true',help='publish the SQLite checkpoint independently of HTML')
    mode.add_argument('--pages-only',action='store_true',help='publish the disposable Pages SQLite read model')
    parser.add_argument('--interval',type=a.positive,help='seconds between completed passes (Pages: 300, catalogue: 1800, HTML: 60)')
    args=parser.parse_args()
    if args.once:
        try:publish_once(args.root,catalogue_only=args.catalogue_only,pages_only=args.pages_only)
        except Exception as error:
            print('provenance checkpoint failed',type(error).__name__,flush=True)
            raise SystemExit(1)
    else:watch(args.root,args.interval or (300 if args.pages_only else (1800 if args.catalogue_only else 60)),catalogue_only=args.catalogue_only,pages_only=args.pages_only)
