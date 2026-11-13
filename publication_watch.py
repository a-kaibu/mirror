"""Drain verified local files independently of source acquisition and retries."""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
from archive_lock import exclusive_file
from bulk import publicly_archived,evict_verified


def publish_pending(store,namespace='vn-archive',bucket='mirrors',profile='hf',limit=None,keep_local=False):
    rows=store.db.execute("SELECT * FROM items WHERE status='verified' ORDER BY size,id").fetchall()
    stats={'processed':0,'published':0,'busy':0,'quarantined':0,'failed':0,'evicted_bytes':0}
    for candidate in rows:
        if limit is not None and stats['processed']>=limit:break
        if publicly_archived(candidate,namespace,bucket):continue
        with ExitStack() as locks:
            try:
                locks.enter_context(exclusive_file(store.root/'.claims'/(hashlib.sha256(candidate['id'].encode()).hexdigest()+'.lock'),blocking=False))
            except BlockingIOError:
                stats['busy']+=1;continue
            row=store.db.execute('SELECT * FROM items WHERE id=?',(candidate['id'],)).fetchone()
            if not row or row['status']!='verified' or publicly_archived(row,namespace,bucket):continue
            if (json.loads(row['data']).get('fields') or {}).get('source_association_quarantined'):
                stats['quarantined']+=1;continue
            stats['processed']+=1
            try:
                stats['published']+=a.publish(store,namespace,bucket,profile,site=row['site'],ident=row['id'],metadata=False)
                if not keep_local:stats['evicted_bytes']+=evict_verified(store,row['id'],namespace,bucket)
            except Exception as error:
                stats['failed']+=1
                # HTTP errors can contain redirected URLs; log identity/type only.
                print('publication deferred',row['id'],type(error).__name__,flush=True)
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive')
    parser.add_argument('--namespace',default='vn-archive');parser.add_argument('--bucket',default='mirrors')
    parser.add_argument('--profile',default='hf');parser.add_argument('--limit',type=a.positive)
    parser.add_argument('--watch',action='store_true');parser.add_argument('--interval',type=a.positive,default=300)
    parser.add_argument('--keep-local',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'verified-publication-watch.lock',blocking=False):
            while True:
                command=[sys.executable,'-u',__file__,'--root',args.root,'--namespace',args.namespace,'--bucket',args.bucket,'--profile',args.profile]
                if args.limit is not None:command+=['--limit',str(args.limit)]
                if args.keep_local:command+=['--keep-local']
                result=subprocess.run(command,stdin=subprocess.DEVNULL)
                print('publication pass exit',result.returncode,flush=True)
                time.sleep(args.interval)
    else:
        try:
            with exclusive_file(Path(args.root)/'.claims'/'verified-publication-pass.lock',blocking=False):
                store=a.Store(args.root)
                try:print('verified publication',publish_pending(store,args.namespace,args.bucket,args.profile,args.limit,args.keep_local),flush=True)
                finally:store.db.close()
        except BlockingIOError:print('verified publication pass already active',flush=True)
