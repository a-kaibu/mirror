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


def run_batch(root,mode,max_pages):
    sources=pending_sources(root,mode)
    if not sources:return 0
    print('metadata frontier batch',mode,[source['key'] for source in sources],flush=True)
    # All archive sources share the crawler's paced archive fetcher. Platform
    # live sources retain their individual host pacing and source claims.
    m.parallel_crawl(root,sources,mode,max_pages)
    return len(sources)


def watch(root,modes,max_pages,interval):
    stop=threading.Event();children={};guard=threading.Lock()
    def halt(*_):
        stop.set()
        with guard:
            for child in children.values():
                if child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,halt);signal.signal(signal.SIGINT,halt)
    def loop(mode):
        while not stop.is_set():
            if pending_sources(root,mode):
                with guard:
                    if stop.is_set():return
                    child=subprocess.Popen([sys.executable,'-u',__file__,'--root',str(root),'--mode',mode,'--batch','--max-pages',str(max_pages)])
                    children[mode]=child
                result=child.wait()
                with guard:children.pop(mode,None)
                print('metadata frontier pass',mode,'exit',result,flush=True)
                if result and stop.wait(interval):return
            elif stop.wait(interval):return
    with exclusive_file(Path(root)/'.claims'/'metadata-frontier-watch.lock',blocking=False):
        with ThreadPoolExecutor(max_workers=len(modes)) as pool:
            for result in pool.map(loop,modes):pass


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive');parser.add_argument('--mode',choices=['both','archive','live'],default='both')
    parser.add_argument('--batch',action='store_true');parser.add_argument('--max-pages',type=a.positive,default=100);parser.add_argument('--interval',type=a.positive,default=60)
    args=parser.parse_args()
    if args.batch and args.mode=='both':parser.error('--batch requires one mode')
    store=a.Store(args.root)
    try:m.initialize(store)
    finally:store.db.close()
    if args.batch:print('metadata sources processed',run_batch(args.root,args.mode,args.max_pages),flush=True)
    else:watch(args.root,['archive','live'] if args.mode=='both' else [args.mode],args.max_pages,args.interval)
