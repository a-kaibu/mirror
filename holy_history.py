"""Discover distinct historical Holyseal product pages from observed URLs."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit,parse_qs
import archiver as a
import holy_inventory as h
import mirror_sources as m
import mirror_history as history
from archive_lock import exclusive_file


def product_url(value):
    if not isinstance(value,str):return None
    source=a.original(value);p=urlsplit(source)
    query=parse_qs(p.query)
    if (p.scheme not in {'http','https'} or p.hostname not in {'holyseal.net','www.holyseal.net'}
            or p.path!='/cgi-bin/mlistview.cgi' or set(query)!={'prdcode'}
            or len(query['prdcode'])!=1 or not query['prdcode'][0].isdigit()):return None
    return source


def candidates(store,limit):
    h.initialize(store);m.initialize(store)
    store.db.execute('CREATE TABLE IF NOT EXISTS mirror_history(site TEXT,url TEXT,checked_at TEXT,status TEXT,data TEXT,error TEXT,PRIMARY KEY(site,url))')
    store.db.commit()
    urls={}
    for row in store.db.execute('SELECT requested_url,chosen_url,status FROM holy_inventory'):
        for value in row[:2]:
            url=product_url(value)
            if url:urls[url]=min(urls.get(url,2),0 if row['status']=='failed' else 1)
    for row in store.db.execute("SELECT url,status FROM mirror_frontier WHERE site='holyseal'"):
        url=product_url(row['url'])
        if url:urls[url]=min(urls.get(url,2),0 if row['status']=='failed' else 1)
    for row in store.db.execute("SELECT data FROM items WHERE site='holyseal'"):
        url=product_url(json.loads(row[0]).get('original_page_url'))
        if url:urls.setdefault(url,2)
    prior={r['url']:r for r in store.db.execute("SELECT * FROM mirror_history WHERE site='holyseal'")}
    # Unseen URLs precede failed lookups. Failed URLs rotate by oldest attempt;
    # successful CDX responses are durable, including an empty capture list.
    eligible=[url for url in urls if url not in prior or prior[url]['status']!='ok']
    eligible.sort(key=lambda url:(url in prior,prior[url]['checked_at'] if url in prior else '',urls[url],url))
    return eligible[:limit]


def collect(store,fetcher,limit=10):
    urls=candidates(store,limit)
    source={**m.definitions()['holyseal'],'homes':[],'seeds':urls}
    return len(urls),history.discover(store,fetcher,source)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive');parser.add_argument('--limit',type=a.positive,default=10)
    parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'holyseal-history-watch.lock',blocking=False):
            while True:
                result=subprocess.run([sys.executable,'-u',__file__,'--root',args.root,'--limit',str(args.limit)])
                print('Holyseal history batch exit',result.returncode,flush=True);time.sleep(60)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'holyseal-history.lock',blocking=False):
            store=a.Store(args.root)
            try:print('Holyseal history coverage',collect(store,a.Fetcher(delay=10,timeout=30,retries=2),args.limit),flush=True)
            finally:store.db.close()
