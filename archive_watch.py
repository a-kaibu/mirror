"""Keep acquiring newly discovered records while source discovery continues."""
import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import archiver as a
import mirror_sources as m
from archive_lock import exclusive_file
from bulk import archive_batch


def queue_refused_replays(store,site):
    """Retry a refused capture on HTTPS, or on HTTP with a publisher digest."""
    rows=store.db.execute("""SELECT DISTINCT c.* FROM wayback_candidates c JOIN items i ON i.id=c.item_id
        JOIN attempts t ON t.item_id=i.id AND t.url=c.url
        WHERE i.site=? AND i.status='failed'
          AND (c.url LIKE 'http://web.archive.org/web/%' OR length(json_extract(i.data,'$.expected_md5'))=32 OR length(json_extract(i.data,'$.expected_sha256'))=64)
          AND (t.error LIKE '<urlopen error [Errno 111] Connection refused%'
            OR t.error LIKE '[Errno 111] Connection refused%')""",(site,)).fetchall()
    for row in rows:
        if not a.binary_archive_capture(row['url']):continue
        alternate=('https://'+row['url'][7:]) if row['url'].startswith('http://') else ('http://'+row['url'][8:])
        store.db.execute('INSERT OR IGNORE INTO wayback_candidates VALUES(?,?,?,?,?)',
            (row['item_id'],alternate,row['original_url'],row['captured_at'],row['index_digest']))



def queue_recovered_mirrors(store,site):
    """Retry an untried observed healthy mirror once; never loop on old failures."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_health' AND type='table'").fetchone():return 0
    if not store.db.execute('SELECT 1 FROM source_health WHERE healthy=1 LIMIT 1').fetchone():return 0
    store.db.execute('CREATE TABLE IF NOT EXISTS recovered_mirror_requeues(item_id TEXT,url TEXT,requeued_at TEXT,PRIMARY KEY(item_id,url))');store.db.commit()
    ids=[r[0] for r in store.db.execute("SELECT id FROM items WHERE site=? AND status='failed'",(site,))]
    count=0
    for ident in ids:
        try:
            with exclusive_file(store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock'),blocking=False):
                row=store.db.execute("SELECT * FROM items WHERE id=? AND status='failed'",(ident,)).fetchone()
                if not row:continue
                record=json.loads(row['data'])
                if not (record.get('expected_md5') or record.get('expected_sha256')):continue
                if (record.get('fields') or {}).get('source_association_quarantined'):continue
                mirrors=record.get('mirrors',[])+a.matching_mirrors(store,row,record)
                fresh=[url for url in a.healthy_source_mirrors(store,site,mirrors)
                    if not store.db.execute('SELECT 1 FROM attempts WHERE item_id=? AND url=? LIMIT 1',(ident,url)).fetchone()
                    and not store.db.execute('SELECT 1 FROM recovered_mirror_requeues WHERE item_id=? AND url=?',(ident,url)).fetchone()]
                if not fresh:continue
                with store.db:
                    changed=store.db.execute("UPDATE items SET status='pending',error=NULL WHERE id=? AND status='failed' AND data=?",(ident,row['data'])).rowcount
                    if changed:
                        store.db.executemany('INSERT INTO recovered_mirror_requeues VALUES(?,?,?)',[(ident,url,a.now()) for url in fresh])
                        count+=1
        except BlockingIOError:continue
    return count


def ready(store,site):
    queue_recovered_mirrors(store,site)
    queue_refused_replays(store,site)
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
