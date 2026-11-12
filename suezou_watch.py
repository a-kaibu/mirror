"""Check Suezo recovery; collect verified catalogue pages and start its archive job."""
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
import archiver as a
import mirror_sources as m

INTERVAL=3*60*60
URL="http://suezou.dyndns.org/"


def probe(fetcher):
    source=m.definitions()["suezou"]
    try:
        with fetcher.open(URL) as response:
            if response.status!=200:return {"healthy":False,"error":"HTTP "+str(response.status)}
            actual=response.geturl();raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024 or not m.allowed(source,actual):return {"healthy":False,"error":"unexpected recovery response"}
        root=a.DOM(a.decode_html(raw)).root
        identity=root.text()+" "+" ".join(n.attrs.get("alt","") for n in root.all({"img"}))
        if not re.search(r"すえぞう|ギャルゲー補完|suezou",identity,re.I):return {"healthy":False,"error":"mirror identity not found"}
        links=[a.resolve(actual,n.attrs.get("href","")) for n in root.all({"a"})]
        if any(u and a.EXT.search(urlsplit(a.original(u)).path) for u in links):return {"healthy":True,"url":actual,"sha256":hashlib.sha256(raw).hexdigest()}
        catalog=[u for u in links if u and m.allowed(source,u) and re.search(r"/(?:bishop|studio-jyaren|key|pulltop|[a-z0-9_-]+)\.html$",urlsplit(u).path,re.I)]
        catalog=[u for u in catalog if not re.search(r"/(?:index|about|contact|info|privacy)\.html$",urlsplit(u).path,re.I)]
        if not catalog:return {"healthy":False,"error":"no publisher catalogue links"}
        with fetcher.open(catalog[0]) as response:
            child_url=response.geturl();child=response.read(4*1024*1024+1)
        if not m.allowed(source,child_url) or len(child)>4*1024*1024:return {"healthy":False,"error":"publisher response invalid"}
        page=a.DOM(a.decode_html(child)).root
        binaries=[a.resolve(child_url,n.attrs.get("href","")) for n in page.all({"a"})]
        if not any(u and a.EXT.search(urlsplit(a.original(u)).path) for u in binaries):return {"healthy":False,"error":"publisher page has no distribution links"}
        return {"healthy":True,"url":actual,"sha256":hashlib.sha256(raw).hexdigest()}
    except (OSError,ValueError,a.http.client.HTTPException) as error:return {"healthy":False,"error":str(error)}


def check(store,fetcher,capture=True,start_worker=True):
    m.initialize(store)
    store.db.execute("CREATE TABLE IF NOT EXISTS source_health(site TEXT PRIMARY KEY,checked_at TEXT NOT NULL,healthy INTEGER NOT NULL,data TEXT NOT NULL)");store.db.commit()
    previous=store.db.execute("SELECT healthy FROM source_health WHERE site='suezou'").fetchone()
    result=probe(fetcher)
    store.db.execute("INSERT OR REPLACE INTO source_health VALUES(?,?,?,?)",("suezou",a.now(),int(result["healthy"]),json.dumps(result,ensure_ascii=False)));store.db.commit()
    print("Suezo health",a.now(),json.dumps(result,ensure_ascii=False),flush=True)
    if result["healthy"] and capture:
        # Refresh live catalogue pages after recovery or once daily; leave the
        # archive-date frontier separate from the current live frontier.
        cutoff=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        if not previous or not previous[0]:
            store.db.execute("UPDATE mirror_frontier SET status='pending' WHERE site='suezou' AND mode='live'")
        else:store.db.execute("UPDATE mirror_frontier SET status='pending' WHERE site='suezou' AND mode='live' AND checked_at<?",(cutoff,))
        store.db.commit()
        m.crawl(store,m.definitions()["suezou"],fetcher,mode="live",max_pages=300)
        if start_worker:subprocess.run(["systemctl","--user","start","--no-block","mirror-suezou-archive.service"],check=True)
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default="archive");parser.add_argument("--once",action="store_true")
    parser.add_argument("--probe-only",action="store_true");parser.add_argument("--capture-only",action="store_true")
    args=parser.parse_args();store=a.Store(args.root)
    try:
        while True:
            start=time.monotonic();check(store,a.Fetcher(delay=3,timeout=15,retries=1),capture=not args.probe_only,start_worker=not args.capture_only)
            if args.once:break
            time.sleep(max(1,INTERVAL-(time.monotonic()-start)))
    finally:store.db.close()
