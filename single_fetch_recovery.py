"""Finish retained delegated transfers without starting new origin downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import archiver as a
from archive_lock import exclusive_file
import single_fetch as transfer

PHASES={'accepted':0,'verified':1,'staged':2,'parts':3,'job':4,'preparing':5}


def pending(store):
    result=[]
    for path in (store.root/'.work/single-fetch').glob('*.json'):
        if path.is_symlink():raise ValueError('unsafe recovery journal')
        state=json.loads(path.read_text());ident=state.get('item_id')
        if not isinstance(ident,str) or path!=transfer.staged.journal_path(store,ident):raise ValueError('recovery journal path identity')
        if state.get('phase') in {'cleaned','aborted'}:continue
        state=transfer._load(store,ident)
        if state['phase'] not in PHASES:continue
        result.append((PHASES[state['phase']],path.stat().st_mtime,ident))
    return [row[2] for row in sorted(result)]


def recover_one(store,ident,*,profile='hf',timeout=30,client=None,execute=None,readback=None):
    claim=store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')
    with exclusive_file(claim,blocking=False):
        state=transfer._load(store,ident)
        if not state or state['phase'] not in PHASES:return None
        client=client or transfer.staging_client(profile,state.get('size'))
        if state['phase']=='accepted':
            # Cleanup must not fall through to a new transfer if metadata has
            # changed since the frozen observation was accepted.
            return transfer.staged.promote(store,client,state['source'],state,state['receipt'],readback=readback)
        if state['phase']=='preparing' or state['phase']=='job' and not state.get('dispatch_attempted_at') and not state.get('run_id'):
            transfer._abort(store,client,state)
            return dict(aborted_unused=True)
        return transfer.run_claimed(store,ident,profile=profile,client=client,execute=execute,readback=readback,timeout=timeout)


def recover_pass(store,*,limit=10,profile='hf',timeout=30):
    if type(limit) is not int or limit<=0:raise ValueError('positive recovery limit required')
    if type(timeout) is not int or not 0<timeout<=1500:raise ValueError('invalid observation window')
    stats=dict(attempted=0,finished=0,busy=0,failed=0,aborted_unused=0)
    for ident in pending(store)[:limit]:
        stats['attempted']+=1
        try:
            result=recover_one(store,ident,profile=profile,timeout=timeout)
            if result:
                stats['aborted_unused']+=int(bool(result.get('aborted_unused')))
                stats['finished']+=int(not result.get('cleanup_pending',False) and not result.get('aborted_unused',False))
        except (BlockingIOError,TimeoutError):stats['busy']+=1
        except Exception as error:
            if store.db.in_transaction:store.db.rollback()
            stats['failed']+=1
            print('single-fetch recovery deferred',ident,type(error).__name__,flush=True)
    return stats


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',default='archive')
    p.add_argument('--limit',type=a.positive,default=10);p.add_argument('--timeout',type=a.positive,default=30)
    p.add_argument('--profile',default='hf');p.add_argument('--watch',action='store_true');p.add_argument('--interval',type=a.positive,default=120)
    args=p.parse_args(argv)
    if args.timeout>1500:p.error('timeout must be at most 1500 seconds')
    root=Path(args.root)
    if args.watch:
        with exclusive_file(root/'.claims/single-fetch-recovery-watch.lock',blocking=False):
            while True:
                job=subprocess.run([sys.executable,'-u',str(Path(__file__).resolve()),'--root',str(root),'--limit',str(args.limit),'--timeout',str(args.timeout),'--profile',args.profile])
                print('single-fetch recovery pass exit',job.returncode,flush=True);time.sleep(args.interval)
    else:
        with exclusive_file(root/'.claims/single-fetch-recovery-pass.lock',blocking=False):
            store=a.Store(root)
            try:print('single-fetch recovery',json.dumps(recover_pass(store,limit=args.limit,profile=args.profile,timeout=args.timeout)),flush=True)
            finally:store.db.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
