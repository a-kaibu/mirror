"""Bounded delegated acquisition passes with a persistent SQLite scan cursor."""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import archiver as a
from archive_lock import exclusive_file
import bulk
import single_fetch as transfer


def _site(site):
    if not isinstance(site,str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",site):
        raise ValueError("invalid source site")
    return site


def _claim(store,site,role):
    return store.root/'.claims'/('delegated-'+hashlib.sha256(site.encode()).hexdigest()+'-'+role+'.lock')


def candidates(store,site,limit):
    """Return only IDs; exhaust the bounded cursor before network or mutations."""
    _site(site)
    if type(limit) is not int or limit<=0:raise ValueError('positive scan limit required')
    with store.db:
        store.db.execute("""CREATE TABLE IF NOT EXISTS delegated_scan_cursor(
            site TEXT PRIMARY KEY,tier INTEGER NOT NULL,item_id TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        store.db.execute("""CREATE TABLE IF NOT EXISTS delegated_host_routes(
            item_id TEXT PRIMARY KEY,data_sha256 TEXT NOT NULL,failed_at TEXT NOT NULL,run_id INTEGER,error TEXT NOT NULL)""")
    saved=store.db.execute('SELECT tier,item_id FROM delegated_scan_cursor WHERE site=?',(site,)).fetchone()
    start=tuple(saved) if saved else (-1,'')
    sql="""SELECT CASE WHEN c.kind IN ('trial','patch') THEN 0 WHEN c.kind='movie' THEN 1 ELSE 2 END tier,i.id
        FROM items i LEFT JOIN item_catalog c ON c.item_id=i.id
        WHERE i.site=? AND i.status='pending' AND i.path IS NULL
        AND (CASE WHEN c.kind IN ('trial','patch') THEN 0 WHEN c.kind='movie' THEN 1 ELSE 2 END,i.id)>(?,?)
        ORDER BY tier,i.id LIMIT ?"""
    rows=store.db.execute(sql,(site,*start,limit)).fetchall()
    if not rows and saved:
        with store.db:store.db.execute('DELETE FROM delegated_scan_cursor WHERE site=?',(site,))
        rows=store.db.execute(sql,(site,-1,'',limit)).fetchall()
    return [(r[0],r[1]) for r in rows]


def advance(store,site,tier,ident):
    with store.db:
        store.db.execute("""INSERT INTO delegated_scan_cursor VALUES(?,?,?,?)
            ON CONFLICT(site) DO UPDATE SET tier=excluded.tier,item_id=excluded.item_id,updated_at=excluded.updated_at""",
            (site,tier,ident,a.now()))


def host_route(store,row):
    """Retain terminal runner evidence and let the ordinary host collector try."""
    state=transfer._load(store,row['id'])
    fingerprint=hashlib.sha256(row['data'].encode()).hexdigest()
    current=store.db.execute('SELECT status,error,data,path,remote_verified_at FROM items WHERE id=?',(row['id'],)).fetchone()
    if (not state or state['phase']!='aborted' or state.get('conclusion') in (None,'success')
        or type(state.get('run_id')) is not int or state['run_id']<=0 or not state.get('dispatch_attempted_at')
        or state.get('data_sha256')!=fingerprint or not current or current['data']!=row['data']
        or current['status']!='failed' or current['path'] or current['remote_verified_at']
        or current['error']!='All eligible observed mirrors failed in delegated transfer'):
        return False
    with store.db:
        changed=store.db.execute("UPDATE items SET status='pending' WHERE id=? AND data=? AND status='failed' AND error=? AND path IS NULL AND remote_verified_at IS NULL",
            (row['id'],row['data'],current['error'])).rowcount
        if not changed:return False
        store.db.execute("""INSERT INTO delegated_host_routes VALUES(?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET data_sha256=excluded.data_sha256,
            failed_at=excluded.failed_at,run_id=excluded.run_id,error=excluded.error""",
            (row['id'],fingerprint,a.now(),state.get('run_id'),current['error']))
    return True


def archive_pass(store,fetcher,*,site,limit=4,scan_limit=100,profile='hf'):
    _site(site)
    if type(limit) is not int or limit<=0:raise ValueError('positive transfer limit required')
    stats=dict(scanned=0,attempted=0,published=0,reused=0,busy=0,ineligible=0,failed=0,host_routes=0)
    with exclusive_file(_claim(store,site,'pass'),blocking=False):
        for tier,ident in candidates(store,site,scan_limit):
            if stats['attempted']>=limit:break
            # A crash or busy claim delays this ID until the next sweep. Any
            # dispatched job is also handled by single_fetch_recovery.
            advance(store,site,tier,ident);stats['scanned']+=1
            with ExitStack() as claims:
                primary=store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')
                try:claims.enter_context(exclusive_file(primary,blocking=False))
                except BlockingIOError:
                    stats['busy']+=1;continue
                row=store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()
                if not row or row['status']!='pending' or row['path']:continue
                saved=store.db.execute('SELECT data_sha256 FROM delegated_host_routes WHERE item_id=?',(ident,)).fetchone()
                if saved and saved[0]==hashlib.sha256(row['data'].encode()).hexdigest():
                    stats['ineligible']+=1;continue
                record=json.loads(row['data'])
                folder=store.root/'files'/row['site']/ident
                if ((record.get('fields') or {}).get('source_association_quarantined')
                    or folder.exists() and any(folder.iterdir())):
                    stats['ineligible']+=1;continue
                try:
                    if bulk.reuse_verified(store,row,'vn-archive','mirrors'):
                        stats['reused']+=1;stats['attempted']+=1;continue
                    if bulk.equivalent_upload_active(store,row,'vn-archive','mirrors'):
                        stats['busy']+=1;continue
                    stats['attempted']+=1
                    result=transfer.archive_candidate(store,row,profile=profile,probe=fetcher.head,
                        range_probe=lambda url,headers:a.open_archive_page(fetcher,url,headers))
                    if result is None:stats['ineligible']+=1
                    elif result.get('accepted_id'):
                        stats['reused' if result.get('reused') else 'published']+=1
                except transfer.SourceTransferFailed:
                    stats['failed']+=1
                    stats['host_routes']+=int(host_route(store,row))
                    print('delegated archive terminal; ordinary route retained',ident,flush=True)
                except (BlockingIOError,TimeoutError):stats['busy']+=1
                except Exception as error:
                    if store.db.in_transaction:store.db.rollback()
                    stats['failed']+=1
                    print('delegated archive deferred',ident,type(error).__name__,flush=True)
    return stats


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='archive');p.add_argument('--site',required=True,type=_site)
    p.add_argument('--limit',default=4,type=a.positive);p.add_argument('--scan-limit',default=100,type=a.positive)
    p.add_argument('--profile',default='hf');p.add_argument('--watch',action='store_true')
    p.add_argument('--interval',default=120,type=a.positive);args=p.parse_args(argv)
    if args.watch:
        class Root:root=Path(args.root)
        with exclusive_file(_claim(Root,args.site,'watch'),blocking=False):
            while True:
                child=subprocess.run([sys.executable,'-u',str(Path(__file__).resolve()),'--root',str(args.root),
                    '--site',args.site,'--limit',str(args.limit),'--scan-limit',str(args.scan_limit),'--profile',args.profile])
                print('delegated archive pass exit',args.site,child.returncode,flush=True)
                time.sleep(args.interval)
    else:
        store=a.Store(args.root)
        try:
            stats=archive_pass(store,a.Fetcher(delay=3,timeout=12,retries=1),site=args.site,
                limit=args.limit,scan_limit=args.scan_limit,profile=args.profile)
            print('delegated archive',args.site,json.dumps(stats),flush=True)
        finally:store.db.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
