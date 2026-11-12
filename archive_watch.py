"""Keep acquiring newly discovered records while source discovery continues."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import archiver as a
import mirror_sources as m
from archive_lock import exclusive_file
from bulk import archive_batch


def ready(store,site):
    # A newly discovered dated binary capture is a reason to retry immediately.
    # Previously failed URLs alone do not cause a tight retry loop.
    store.db.execute("""UPDATE items SET status='pending' WHERE site=? AND status='failed'
        AND EXISTS(SELECT 1 FROM wayback_candidates c WHERE c.item_id=items.id
          AND NOT EXISTS(SELECT 1 FROM attempts t WHERE t.item_id=items.id AND t.url=c.url))""",(site,))
    store.db.commit()
    return store.db.execute("SELECT count(*) FROM items WHERE site=? AND status IN ('pending','verified')",(site,)).fetchone()[0]


def watch(root,site,stop,interval=60):
    with exclusive_file(Path(root)/'.claims'/('watch-'+site+'.lock')):
        store=a.Store(root)
        try:
            while not stop.is_set():
                try:
                    if ready(store,site):
                        print('archive watcher work',site,flush=True)
                        archive_batch(store,a.Fetcher(delay=10,timeout=30,retries=2,tls_pins=a.source_tls_pins()),site=site,checkpoint_every=25)
                except Exception as error:
                    print('archive watcher failed',site,type(error).__name__,str(error),flush=True)
                    # Publication errors preserve verified working copies; retry
                    # after cooldown instead of downloading an unpublishable queue.
                    if stop.wait(max(interval,300)):break
                if stop.wait(interval):break
        finally:store.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--site',action='append');args=parser.parse_args()
    sites=args.site or [key for key,source in m.definitions().items() if source['adapter']!='native']
    invalid=set(sites)-set(m.definitions())-{'official'}
    if invalid:parser.error('unregistered source: '+','.join(sorted(invalid)))
    stop=threading.Event();pool=ThreadPoolExecutor(max_workers=max(1,len(sites)),thread_name_prefix='archive-watch')
    jobs=[pool.submit(watch,args.root,site,stop) for site in dict.fromkeys(sites)]
    try:
        for job in jobs:job.result()
    except KeyboardInterrupt:
        stop.set();raise
    finally:pool.shutdown(wait=True,cancel_futures=True)
