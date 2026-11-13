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


def probe(fetcher, catalog_offset=0, save_page=None):
    source=m.definitions()["suezou"]
    def read(url,limit):
        with fetcher.open(url) as response:
            actual=response.geturl();raw=response.read(limit+1);status=response.status
            charset=response.headers.get_content_charset()
        if len(raw)>limit or not m.allowed(source,actual):raise ValueError("unexpected recovery response")
        if save_page:save_page(actual,raw,status)
        if status!=200:raise ValueError("HTTP "+str(status))
        return actual,raw,charset
    try:
        actual,raw,charset=read(URL,2*1024*1024)
        root=a.DOM(a.decode_html(raw,charset)).root
        identity=root.text()+" "+" ".join(n.attrs.get("alt","") for n in root.all({"img"}))
        if not re.search(r"すえぞう|ギャルゲー補完|suezou",identity,re.I):return {"healthy":False,"error":"mirror identity not found"}
        links=[a.resolve(actual,n.attrs.get("href","")) for n in root.all({"a"})]
        if any(u and a.EXT.search(urlsplit(a.original(u)).path) for u in links):return {"healthy":True,"url":actual,"sha256":hashlib.sha256(raw).hexdigest()}
        catalog=[u for u in links if u and m.allowed(source,u) and re.search(r"/[a-z0-9_-]+\.html$",urlsplit(u).path,re.I)]
        catalog=list(dict.fromkeys(u for u in catalog if not re.search(r"/(?:index|about|contact|info|privacy)\.html$",urlsplit(u).path,re.I)))
        if not catalog:return {"healthy":False,"error":"no publisher catalogue links"}
        checks=[];offset=catalog_offset%len(catalog)
        # Bound each scheduled check, but rotate so an empty first brand does
        # not prevent discovery of a recovered catalogue elsewhere on the site.
        for step in range(min(3,len(catalog))):
            target=catalog[(offset+step)%len(catalog)]
            next_offset=(offset+step+1)%len(catalog)
            try:
                child_url,child,charset=read(target,4*1024*1024)
                page=a.DOM(a.decode_html(child,charset)).root
                binaries=[a.resolve(child_url,n.attrs.get("href","")) for n in page.all({"a"})]
                found=any(u and a.EXT.search(urlsplit(a.original(u)).path) for u in binaries)
                checks.append({"url":target,"response_url":child_url,"distribution_links":found})
                if found:return {"healthy":True,"url":actual,"sha256":hashlib.sha256(raw).hexdigest(),"publisher_checks":checks,"next_catalog_offset":next_offset}
            except (OSError,ValueError,a.http.client.HTTPException) as error:
                if isinstance(error,a.HTTPError):error.close()
                checks.append({"url":target,"error":str(error)})
        return {"healthy":False,"error":"checked publisher pages have no distribution links","publisher_checks":checks,"next_catalog_offset":next_offset}
    except (OSError,ValueError,a.http.client.HTTPException) as error:
        if isinstance(error,a.HTTPError):error.close()
        return {"healthy":False,"error":str(error)}


def check(store,fetcher,capture=True,start_worker=True):
    m.initialize(store)
    store.db.execute("CREATE TABLE IF NOT EXISTS source_health(site TEXT PRIMARY KEY,checked_at TEXT NOT NULL,healthy INTEGER NOT NULL,data TEXT NOT NULL)");store.db.commit()
    store.db.execute("CREATE TABLE IF NOT EXISTS source_health_history(site TEXT,checked_at TEXT,healthy INTEGER,data TEXT,PRIMARY KEY(site,checked_at))");store.db.commit()
    previous=store.db.execute("SELECT healthy,data FROM source_health WHERE site='suezou'").fetchone()
    offset=json.loads(previous['data']).get('next_catalog_offset',0) if previous else 0
    pages=[]
    def save_page(url,raw,status):
        digest=hashlib.sha256(raw).hexdigest();rel='html/health/'+digest+'.html'
        path=store.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        pages.append({'url':url,'path':rel,'sha256':digest,'status':status})
    result=probe(fetcher,catalog_offset=offset,save_page=save_page)
    result.setdefault('next_catalog_offset',offset)
    if pages:result['pages']=pages
    stamp=a.now();encoded=json.dumps(result,ensure_ascii=False)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO source_health VALUES(?,?,?,?)",("suezou",stamp,int(result["healthy"]),encoded))
        store.db.execute("INSERT INTO source_health_history VALUES(?,?,?,?)",("suezou",stamp,int(result["healthy"]),encoded))
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
