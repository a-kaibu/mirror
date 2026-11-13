"""Retry temporary download failures with recorded, bounded exponential cooldowns."""
import argparse
from datetime import datetime,timezone
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import time
import archiver as a
from archive_lock import exclusive_file


def temporary(error):
    return bool(error and (re.match(r'^HTTP Error (?:408|425|429|500|502|503|504):',error)
        or error in {'timed out','The read operation timed out','<urlopen error timed out>','Remote end closed connection without response'}
        or re.match(r'^(?:<urlopen error )?\[Errno (?:104|-3)\] (?:Connection reset by peer|Temporary failure in name resolution)',error)))


def recover(store,at=None,cooldown=3*3600):
    if cooldown<=0:raise ValueError('cooldown must be positive')
    at=at or datetime.now(timezone.utc)
    store.db.execute('CREATE TABLE IF NOT EXISTS transient_retries(item_id TEXT,attempted_at TEXT,requeued_at TEXT,reason TEXT,PRIMARY KEY(item_id,attempted_at))');store.db.commit()
    stats={'requeued':0,'deferred':0,'cooling':0}
    rows=store.db.execute("SELECT id,error FROM items WHERE status='failed'").fetchall()
    for candidate in rows:
        ident=candidate['id']
        try:
            with exclusive_file(store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock'),blocking=False):
                row=store.db.execute("SELECT error FROM items WHERE id=? AND status='failed'",(ident,)).fetchone()
                if not row:continue
                attempts=store.db.execute('SELECT attempted_at,error FROM attempts WHERE item_id=? AND id IN (SELECT max(id) FROM attempts WHERE item_id=? GROUP BY url)',(ident,ident)).fetchall()
                transient=[attempt for attempt in attempts if temporary(attempt['error'])]
                if not transient:continue
                last_attempt=max(attempt['attempted_at'] for attempt in attempts)
                reason=max(transient,key=lambda attempt:attempt['attempted_at'])['error']
                try:last=datetime.fromisoformat(last_attempt)
                except ValueError:continue
                if last.tzinfo is None:continue
                retries=store.db.execute('SELECT count(*) FROM transient_retries WHERE item_id=?',(ident,)).fetchone()[0]
                if (at-last).total_seconds()<cooldown*2**min(retries,3):stats['cooling']+=1;continue
                if store.db.execute('SELECT 1 FROM transient_retries WHERE item_id=? AND attempted_at=?',(ident,last_attempt)).fetchone():continue
                with store.db:
                    changed=store.db.execute("UPDATE items SET status='pending',error=NULL WHERE id=? AND status='failed' AND error IS ? AND (SELECT max(attempted_at) FROM attempts WHERE item_id=items.id)=?",(ident,row['error'],last_attempt)).rowcount
                    if changed:
                        store.db.execute('INSERT INTO transient_retries VALUES(?,?,?,?)',(ident,last_attempt,at.isoformat(),reason))
                        stats['requeued']+=1
        except BlockingIOError:stats['deferred']+=1
    if stats['requeued']:store.export()
    return stats


def recover_frontiers(store,at=None,cooldown=3*3600):
    """Requeue transient HTML failures without competing with active crawlers."""
    if cooldown<=0:raise ValueError('cooldown must be positive')
    at=at or datetime.now(timezone.utc)
    stats={'requeued':0,'deferred':0,'cooling':0}
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='mirror_frontier'").fetchone():return stats
    store.db.execute("""CREATE TABLE IF NOT EXISTS frontier_transient_retries(
        site TEXT,url TEXT,mode TEXT,failed_at TEXT,requeued_at TEXT,reason TEXT,
        PRIMARY KEY(site,url,mode,failed_at))""");store.db.commit()
    # Retain only small transient candidates, and finish the reader before any
    # write so another worker's commit cannot invalidate a read snapshot.
    cursor=store.db.execute("SELECT site,url,mode,error,checked_at FROM mirror_frontier WHERE status='failed'")
    try:candidates=[dict(row) for row in cursor if temporary(row['error'])]
    finally:cursor.close()
    for site in sorted({row['site'] for row in candidates}):
        try:
            with exclusive_file(store.root/'.claims'/('discovery-'+site+'.lock'),blocking=False):
                for candidate in (r for r in candidates if r['site']==site):
                    key=(site,candidate['url'],candidate['mode'])
                    row=store.db.execute("SELECT error,checked_at FROM mirror_frontier WHERE site=? AND url=? AND mode=? AND status='failed'",key).fetchone()
                    if not row or not temporary(row['error']):continue
                    try:last=datetime.fromisoformat(row['checked_at'])
                    except (ValueError,TypeError):continue
                    if last.tzinfo is None:continue
                    retries=store.db.execute('SELECT count(*) FROM frontier_transient_retries WHERE site=? AND url=? AND mode=?',key).fetchone()[0]
                    if (at-last).total_seconds()<cooldown*2**min(retries,3):stats['cooling']+=1;continue
                    if store.db.execute('SELECT 1 FROM frontier_transient_retries WHERE site=? AND url=? AND mode=? AND failed_at=?',(*key,row['checked_at'])).fetchone():continue
                    with store.db:
                        changed=store.db.execute("UPDATE mirror_frontier SET status='pending',error=NULL WHERE site=? AND url=? AND mode=? AND status='failed' AND checked_at=? AND error IS ?",(*key,row['checked_at'],row['error'])).rowcount
                        if changed:
                            store.db.execute('INSERT INTO frontier_transient_retries VALUES(?,?,?,?,?,?)',(*key,row['checked_at'],at.isoformat(),row['error']))
                            stats['requeued']+=1
        except BlockingIOError:stats['deferred']+=sum(r['site']==site for r in candidates)
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'transient-recovery-watch.lock',blocking=False):
            while True:
                result=subprocess.run([sys.executable,'-u',__file__,'--root',args.root])
                print('transient recovery pass exit',result.returncode,flush=True);time.sleep(300)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'transient-recovery.lock',blocking=False):
            store=a.Store(args.root)
            try:
                # Fresh child passes pick up recovered hosts without interrupting
                # productive long-lived archive watchers or their transfers.
                from archive_watch import queue_recovered_mirrors
                sites=[row[0] for row in store.db.execute("SELECT DISTINCT site FROM items WHERE status='failed'")]
                recovered=sum(queue_recovered_mirrors(store,site) for site in sites)
                print('recovered mirror requeues',recovered,flush=True)
                print('temporary failure recovery',recover(store),flush=True)
                print('temporary HTML recovery',recover_frontiers(store),flush=True)
            finally:store.db.close()
