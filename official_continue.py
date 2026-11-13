"""Continue publisher crawls whose observed page queue exceeded a batch bound."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
import official as o
from archive_lock import exclusive_file

MARKER='page bound reached; needs continuation'


def collect(root,limit=10,workers=3,fetcher=None):
    store=a.Store(root)
    try:
        o.initialize(store);seeds={game['id']:(game,brand,seed) for game,brand,seed in o.seeds(store)}
        rows=[]
        for row in store.db.execute("SELECT * FROM official_scans WHERE status='review' ORDER BY updated_at,game_id"):
            errors=json.loads(row['error'] or '[]')
            if row['game_id'] in seeds and any(error.get('error')==MARKER for error in errors):rows.append(row['game_id'])
            if len(rows)>=limit:break
    finally:store.db.close()
    fetcher=fetcher or o.HostFetcher()
    def run(ident):
        try:
            with exclusive_file(Path(root)/'.claims'/('official-scan-'+str(ident)+'.lock'),blocking=False):
                local=a.Store(root)
                try:
                    row=local.db.execute('SELECT * FROM official_scans WHERE game_id=?',(ident,)).fetchone()
                    errors=json.loads(row['error'] or '[]')
                    marker=next((error for error in errors if error.get('error')==MARKER),None)
                    if marker is None:return 0
                    game,brand,seed=seeds[ident]
                    continuation=None
                    if marker.get('pending') and marker.get('seed')==seed:
                        continuation={**marker,'found':row['found'],'failures':[error for error in errors if error.get('error')!=MARKER]}
                    o.scan(local,fetcher,game,brand,seed,continuation=continuation)
                    return 1
                finally:local.db.close()
        except BlockingIOError:
            print('official continuation deferred busy product',ident,flush=True);return 0
    with ThreadPoolExecutor(max_workers=workers) as pool:return sum(pool.map(run,rows))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive')
    parser.add_argument('--limit',type=a.positive,default=10);parser.add_argument('--workers',type=a.positive,default=3);parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'official-continuation-watch.lock',blocking=False):
            while True:
                result=subprocess.run([sys.executable,'-u',__file__,'--root',args.root,'--limit',str(args.limit),'--workers',str(args.workers)])
                print('official continuation pass exit',result.returncode,flush=True);time.sleep(60)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'official-continuation.lock',blocking=False):
            print('official continuation products',collect(args.root,args.limit,args.workers),flush=True)
