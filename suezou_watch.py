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
from archive_lock import exclusive_file

INTERVAL=3*60*60
URL="http://suezou.dyndns.org/"


def catalogue_links(root,page):
    source=m.definitions()['suezou'];result=[]
    for node in root.all({'a','frame','iframe'}):
        target=a.resolve(page,node.attrs.get('href') or node.attrs.get('src',''))
        if not target or a.WAYBACK.match(target) or not m.allowed(source,target):continue
        path=urlsplit(target).path
        if not re.search(r'\.html?$',path,re.I):continue
        if re.search(r'/(?:index|about|contact|info|privacy|profile|information)\.html?$',path,re.I):continue
        result.append(target)
    return list(dict.fromkeys(result))


def probe(fetcher, catalog_offset=0, save_page=None, catalog_urls=None):
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
        prior=[]
        for target in catalog_urls or []:
            if isinstance(target,str) and not a.WAYBACK.match(target) and m.allowed(source,target) and urlsplit(target).scheme in {'http','https'} and re.search(r'\.html?$',urlsplit(target).path,re.I):prior.append(target)
        catalog=list(dict.fromkeys(prior+catalogue_links(root,actual)))
        if not catalog:return {'healthy':False,'error':'no publisher catalogue links','catalog_urls':[]}
        checks=[];cursor=catalog_offset%len(catalog);checked=set();priority=[]
        # Prefer observed descendants within the same three-child budget.
        # Otherwise a root containing many yearly indexes delays all actual
        # publisher pages for many scheduled checks, despite a reachable site.
        def resume_cursor():
            pending=next((url for url in priority if url not in checked),None)
            return catalog.index(pending) if pending else cursor
        for step in range(3):
            while priority and priority[0] in checked:priority.pop(0)
            if priority:
                target=priority.pop(0);index=catalog.index(target)
            else:
                choices=[(cursor+delta)%len(catalog) for delta in range(len(catalog)) if catalog[(cursor+delta)%len(catalog)] not in checked]
                if not choices:break
                index=choices[0];target=catalog[index]
            checked.add(target)
            try:
                child_url,child,charset=read(target,4*1024*1024)
                page=a.DOM(a.decode_html(child,charset)).root
                children=catalogue_links(page,child_url)
                for discovered in children:
                    if discovered not in catalog:catalog.append(discovered)
                priority=list(dict.fromkeys([url for url in children if url not in checked]+priority))
                binaries=[a.resolve(child_url,n.attrs.get('href','')) for n in page.all({'a'})]
                found=any(u and a.EXT.search(urlsplit(a.original(u)).path) for u in binaries)
                checks.append({'url':target,'response_url':child_url,'distribution_links':found})
                cursor=(index+1)%len(catalog)
                if found:return {'healthy':True,'url':actual,'sha256':hashlib.sha256(raw).hexdigest(),'publisher_checks':checks,'next_catalog_offset':resume_cursor(),'catalog_urls':catalog}
            except (OSError,ValueError,a.http.client.HTTPException) as error:
                if isinstance(error,a.HTTPError):error.close()
                checks.append({'url':target,'error':str(error)})
                cursor=(index+1)%len(catalog)
        return {'healthy':False,'error':'checked publisher pages have no distribution links','publisher_checks':checks,'next_catalog_offset':resume_cursor(),'catalog_urls':catalog}

    except (OSError,ValueError,a.http.client.HTTPException) as error:
        if isinstance(error,a.HTTPError):error.close()
        return {"healthy":False,"error":str(error)}


def check(store,fetcher,capture=True,start_worker=True):
    m.initialize(store)
    store.db.execute("CREATE TABLE IF NOT EXISTS source_health(site TEXT PRIMARY KEY,checked_at TEXT NOT NULL,healthy INTEGER NOT NULL,data TEXT NOT NULL)");store.db.commit()
    store.db.execute("CREATE TABLE IF NOT EXISTS source_health_history(site TEXT,checked_at TEXT,healthy INTEGER,data TEXT,PRIMARY KEY(site,checked_at))");store.db.commit()
    previous=store.db.execute("SELECT healthy,data FROM source_health WHERE site='suezou'").fetchone()
    offset=json.loads(previous['data']).get('next_catalog_offset',0) if previous else 0
    prior_urls=json.loads(previous['data']).get('catalog_urls',[]) if previous else []
    pages=[]
    def save_page(url,raw,status):
        digest=hashlib.sha256(raw).hexdigest();rel='html/health/'+digest+'.html'
        path=store.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        pages.append({'url':url,'path':rel,'sha256':digest,'status':status})
    result=probe(fetcher,catalog_offset=offset,save_page=save_page,catalog_urls=prior_urls)
    result.setdefault('next_catalog_offset',offset)
    result.setdefault('catalog_urls',prior_urls)
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
        try:
            with exclusive_file(store.root/'.claims'/'discovery-suezou.lock',blocking=False):
                if not previous or not previous[0]:
                    store.db.execute("UPDATE mirror_frontier SET status='pending' WHERE site='suezou' AND mode='live'")
                else:store.db.execute("UPDATE mirror_frontier SET status='pending' WHERE site='suezou' AND mode='live' AND checked_at<?",(cutoff,))
                store.db.commit()
                m.crawl(store,m.definitions()["suezou"],fetcher,mode="live",max_pages=300)
        except BlockingIOError:
            print('Suezo catalogue refresh deferred: discovery already active',flush=True)
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
