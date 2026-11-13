"""Continue saved mirror frontiers in bounded fresh-process batches."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import archiver as a
import mirror_sources as m
from archive_lock import exclusive_file


def pending_sources(root,mode):
    db=sqlite3.connect((Path(root)/'index.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    try:counts={(site,kind):count for site,kind,count in db.execute("SELECT site,mode,count(*) FROM mirror_frontier WHERE status='pending' GROUP BY site,mode")}
    finally:db.close()
    sources=[]
    for key,source in m.definitions().items():
        if source['adapter'] in {'native','curated'} or not counts.get((key,mode)):continue
        if mode=='archive' and source['adapter']=='platform' and counts.get((key,'live')):continue
        try:
            with exclusive_file(Path(root)/'.claims'/('discovery-'+key+'.lock'),blocking=False):pass
        except BlockingIOError:continue
        sources.append(source)
    return sources


def run_batch(root,mode,max_pages,workers=1):
    sources=pending_sources(root,mode)
    if not sources:return 0
    print('metadata frontier batch',mode,[source['key'] for source in sources],flush=True)
    # All archive sources share the crawler's paced archive fetcher. Platform
    # live sources retain their individual host pacing and source claims.
    m.parallel_crawl(root,sources,mode,max_pages,workers=workers)
    return len(sources)


def watch(root,modes,max_pages,interval,workers=1,parallel_modes=False):
    stop=threading.Event();children={};guard=threading.RLock()
    def halt(*_):
        stop.set()
        with guard:
            for child in children.values():
                if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,halt);signal.signal(signal.SIGINT,halt)
    def run_one(mode):
        if not pending_sources(root,mode):return None
        with guard:
            if stop.is_set():return None
            child=subprocess.Popen([sys.executable,'-u',__file__,'--root',str(root),'--mode',mode,'--batch','--max-pages',str(max_pages),'--workers',str(workers)])
            children[mode]=child
            # A signal can arrive during Popen before the child is registered.
            if stop.is_set() and child.poll() is None:child.terminate()
        result=child.wait()
        with guard:children.pop(mode,None)
        print('metadata frontier pass',mode,'exit',result,flush=True)
        return result
    def loop(mode):
        while not stop.is_set():
            result=run_one(mode)
            if (result is None or result) and stop.wait(interval):return
    with exclusive_file(Path(root)/'.claims'/'metadata-frontier-watch.lock',blocking=False):
        if parallel_modes and len(modes)>1:
            with ThreadPoolExecutor(max_workers=len(modes)) as pool:
                for result in pool.map(loop,modes):pass
        else:
            # Bound total HTML batch memory, while giving every mode a turn.
            while not stop.is_set():
                progressed=failed=False
                for mode in modes:
                    if stop.is_set():break
                    result=run_one(mode)
                    progressed=progressed or result is not None
                    failed=failed or bool(result)
                if (not progressed or failed) and stop.wait(interval):return


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive');parser.add_argument('--mode',choices=['both','archive','live'],default='both')
    parser.add_argument('--batch',action='store_true');parser.add_argument('--max-pages',type=a.positive,default=10);parser.add_argument('--interval',type=a.positive,default=60)
    parser.add_argument('--workers',type=a.positive,default=1,help='concurrent source crawls in a batch')
    parser.add_argument('--parallel-modes',action='store_true',help='run archive and live batches concurrently; increases memory use')
    args=parser.parse_args()
    if args.batch and args.mode=='both':parser.error('--batch requires one mode')
    store=a.Store(args.root)
    try:m.initialize(store)
    finally:store.db.close()
    if args.batch:print('metadata sources processed',run_batch(args.root,args.mode,args.max_pages,args.workers),flush=True)
    else:watch(args.root,['archive','live'] if args.mode=='both' else [args.mode],args.max_pages,args.interval,args.workers,args.parallel_modes)
