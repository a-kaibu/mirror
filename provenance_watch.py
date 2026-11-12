"""Publish saved HTML independently of body-transfer catalogue checkpoints."""
import argparse
from pathlib import Path
import time
import archiver as a
from archive_lock import exclusive_file

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--once',action='store_true');args=parser.parse_args()
    with exclusive_file(Path(args.root)/'.claims'/'provenance-watch.lock'):
        store=a.Store(args.root)
        try:
            while True:
                try:a.publish_metadata(store,'vn-archive','mirrors')
                except Exception as error:
                    print('provenance checkpoint failed',type(error).__name__,str(error),flush=True)
                    if args.once:raise
                    time.sleep(60);continue
                if args.once:break
                time.sleep(60)
        finally:store.db.close()
