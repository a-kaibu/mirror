"""Continue Holyseal native captured pages and untried catalogue product IDs."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit,parse_qs
import archiver as a
import holy_inventory as h
import mirror_sources as m
from archive_lock import exclusive_file


def collect_frontier(store,fetcher,limit=50):
    m.initialize(store);stats={'captured':0,'failed':0,'review':0}
    rows=store.db.execute("SELECT * FROM mirror_frontier WHERE site='holyseal' AND mode='archive' AND status='pending' ORDER BY rowid LIMIT ?",(limit,)).fetchall()
    for row in rows:
        url=row['url'];match=a.WAYBACK.match(url);source=urlsplit(a.original(url));code=parse_qs(source.query).get('prdcode',[''])[0]
        valid=bool(match and re.fullmatch(r'\d{14}',match[1]) and source.hostname in {'holyseal.net','www.holyseal.net'} and source.path=='/cgi-bin/mlistview.cgi' and code.isdigit())
        if not valid:
            status='review';error='not an observed Holyseal product replay URL';actual=None;path=None
        else:
            failures=a.crawl(store,fetcher,'holyseal',url,max_items=1,max_pages=1,export=False)
            saved=store.db.execute('SELECT * FROM pages WHERE url=?',(a.canonical(url),)).fetchone()
            status='failed' if failures else 'captured'
            error=(saved['error'] if saved else 'capture needs parser/network review') if failures else None
            actual=saved['response_url'] if saved else None;path=saved['path'] if saved else None
        store.db.execute("UPDATE mirror_frontier SET status=?,capture_url=?,path=?,checked_at=?,error=? WHERE site='holyseal' AND url=? AND mode='archive'",(status,actual,path,a.now(),error,url));store.db.commit()
        stats[status]+=1
        print('Holyseal native frontier',status,url,flush=True)
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--limit',type=a.positive,default=50);parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'holyseal-native-watch.lock',blocking=False):
            while True:
                result=subprocess.run([sys.executable,'-u',__file__,'--root',args.root,'--limit',str(args.limit)])
                print('Holyseal native batch exit',result.returncode,flush=True);time.sleep(60)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'discovery-holyseal.lock',blocking=False):
            store=a.Store(args.root)
            try:
                fetcher=a.Fetcher(delay=10,timeout=30,retries=2)
                print('Holyseal captured-page coverage',collect_frontier(store,fetcher,args.limit),flush=True)
                print('Holyseal untried product coverage',h.collect(store,fetcher,limit=args.limit),flush=True)
            finally:store.db.close()
