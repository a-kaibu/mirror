"""Registered mirror discovery with persistent archive/live frontiers."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
import archiver as a


def definitions():
    return {s["key"]:s for s in tomllib.loads(Path(__file__).with_name("mirror_sources.toml").read_text())["sources"]}


def clean(url):
    p=urlsplit(a.original(url))
    query=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.startswith("utm_") and k not in {"_gl","gclid","fbclid","PHPSESSID"}]
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode(query),""))


def initialize(store):
    store.db.executescript("""
      CREATE TABLE IF NOT EXISTS mirror_sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS mirror_frontier(site TEXT,url TEXT,mode TEXT,status TEXT NOT NULL DEFAULT 'pending',capture_url TEXT,path TEXT,checked_at TEXT,error TEXT,PRIMARY KEY(site,url,mode));
      CREATE INDEX IF NOT EXISTS mirror_pending ON mirror_frontier(site,mode,status);
      CREATE TABLE IF NOT EXISTS mirror_links(site TEXT,parent_url TEXT,url TEXT,label TEXT,kind TEXT,matched_item TEXT,seen_at TEXT,PRIMARY KEY(site,parent_url,url));
    """)
    for key,source in definitions().items():store.db.execute("INSERT OR REPLACE INTO mirror_sources VALUES(?,?,?)",(key,source["name"],json.dumps(source,ensure_ascii=False)))
    store.db.commit()


def allowed(source,url):
    host=urlsplit(a.original(url)).hostname
    return bool(host and any(host==urlsplit(u).hostname for u in source["homes"]+source["seeds"]))


def enqueue(store,source,mode,urls=None):
    for url in urls or source["seeds"]+source["homes"]:
        if urlsplit(url).scheme in {"http","https"}:
            store.db.execute("INSERT OR IGNORE INTO mirror_frontier(site,url,mode) VALUES(?,?,?)",(source["key"],clean(url),mode))
    store.db.commit()


def navigable(source,url):
    path=urlsplit(a.original(url)).path
    key=source["key"]
    if key=="moepedia":return path=="/" or path.startswith("/trial/") or bool(re.fullmatch(r"/game/\d+/?",path))
    if key=="getchu":return path=="/" or path.endswith("/soft.phtml")
    if key=="dmm":return path=="/" or "/detail/" in path
    if key=="dlsite":return path=="/" or "/work/" in path
    return True


def seed_platform(store,source,mode):
    key=source["key"]
    if key not in {"getchu","dmm","dlsite"}:return
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='external_metadata'").fetchone():return
    from urllib.parse import quote,parse_qs
    rows=store.db.execute("SELECT data FROM external_metadata WHERE provider='erogamescape' AND entity='game' ORDER BY external_id DESC").fetchall()
    for row in rows:
        game=json.loads(row[0]);url=None
        if key=="getchu" and type(game.get("comike")) is int and game["comike"]>0:
            url=f"https://www.getchu.com/soft.phtml?id={game['comike']}"
        elif key=="dmm" and game.get("dmm"):
            trial=game.get("trial_url") or ""
            if urlsplit(trial).hostname=="al.fanza.co.jp":trial=parse_qs(urlsplit(trial).query).get("lurl",[""])[0]
            if trial and allowed(source,trial) and "/detail/" in urlsplit(trial).path:url=trial
            else:
                host="www.dmm.co.jp" if game.get("erogame") is not False else "www.dmm.com"
                genre=game.get("dmm_genre") or "digital";category=game.get("dmm_genre_2") or "pcgame"
                url=f"https://{host}/{quote(genre,safe='')}/{quote(category,safe='')}/-/detail/=/cid={quote(str(game['dmm']),safe='')}/"
        elif key=="dlsite" and game.get("dlsite_id"):
            category=game.get("dlsite_domain") or ("pro" if str(game["dlsite_id"]).startswith("VJ") else "maniax")
            url=f"https://www.dlsite.com/{quote(str(category),safe='')}/work/=/product_id/{quote(str(game['dlsite_id']),safe='')}.html"
        if url and allowed(source,url):
            store.db.execute("INSERT OR IGNORE INTO mirror_frontier(site,url,mode) VALUES(?,?,?)",(key,clean(url),mode))
    store.db.commit()


def availability(fetcher,url,source):
    endpoint="https://archive.org/wayback/available?"+urlencode({"url":url})
    with fetcher.open(endpoint) as response:raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError("availability response too large")
    payload=json.loads(raw)
    if not isinstance(payload,dict):raise ValueError("availability response is not an object")
    snapshots=payload.get("archived_snapshots") or {}
    if not isinstance(snapshots,dict):raise ValueError("invalid snapshot data")
    closest=snapshots.get("closest") or {}
    if not isinstance(closest,dict):raise ValueError("invalid closest snapshot")
    if closest.get("available") is not True or str(closest.get("status"))!="200":return None
    capture=closest.get("url","")
    if not a.WAYBACK.match(capture) or not allowed(source,capture):raise ValueError("capture outside registered mirror")
    return a.canonical(capture)


def known_files(store,site):
    by_hash={};by_url={}
    for row in store.rows():
        if row["site"]==site:continue
        data=json.loads(row["data"])
        if data.get("expected_md5"):by_hash.setdefault(data["expected_md5"],[]).append(data)
        for url in data["mirrors"]:by_url.setdefault(clean(url),[]).append(data)
    return by_hash,by_url


def match_records(store,source,html,page,index):
    hashes,urls=index;found=0
    for candidate in a.parse_detail(html,page,source["key"]):
        donors=hashes.get(candidate.get("expected_md5"),[]) if candidate.get("expected_md5") else []
        if not donors and not candidate.get("expected_md5"):
            donors=[d for url in candidate["mirrors"] for d in urls.get(clean(url),[])
                if candidate.get("expected_size") is None or d.get("expected_size") is None or candidate["expected_size"]==d["expected_size"]]
        products={(d.get("brand"),d["title"]) for d in donors}
        if len(products)!=1:continue
        donor=sorted(donors,key=lambda d:d["id"])[0]
        mirrors=[]
        for url in candidate["mirrors"]:
            stamp=a.WAYBACK.match(url) or a.WAYBACK.match(page)
            if stamp:mirrors.append(a.replay(url,stamp[1],raw=True))
            mirrors.append(a.original(url))
        record={**donor,"id":hashlib.sha256((source["key"]+"\n"+a.original(page)+"\n"+candidate["filename"]).encode()).hexdigest(),
            "site":source["key"],"page_url":page,"original_page_url":a.original(page),"filename":candidate["filename"],"mirrors":list(dict.fromkeys(mirrors)),
            "fields":{**donor.get("fields",{}),"matched_source_id":donor["id"],"mirror_source":source["name"]}}
        store.add(record);found+=1
        for url in candidate["mirrors"]:
            store.db.execute("UPDATE mirror_links SET matched_item=? WHERE site=? AND parent_url=? AND url=?",(record["id"],source["key"],page,clean(url)))
    store.db.commit();return found


def crawl(store,source,fetcher,mode="archive",max_pages=100):
    initialize(store);enqueue(store,source,mode);seed_platform(store,source,mode)
    index=known_files(store,source["key"]);processed=found=0
    while processed<max_pages:
        row=store.db.execute("SELECT * FROM mirror_frontier WHERE site=? AND mode=? AND status='pending' ORDER BY rowid LIMIT 1",(source["key"],mode)).fetchone()
        if not row:break
        url=row["url"];processed+=1;actual=None;rel=None
        try:
            target=url if mode=="live" else availability(fetcher,url,source)
            if target is None:
                store.db.execute("UPDATE mirror_frontier SET status='no_capture_returned',checked_at=? WHERE site=? AND url=? AND mode=?",(a.now(),source["key"],url,mode));store.db.commit();print("mirror",source["key"],"no capture",url,flush=True);continue
            with fetcher.open(target) as response:
                actual=a.canonical(response.geturl())
                if not allowed(source,actual):raise ValueError("mirror redirected outside registered hosts")
                if mode=="archive" and not a.WAYBACK.match(actual):raise ValueError("archive left Wayback")
                raw=response.read(16*1024*1024+1);charset=response.headers.get_content_charset()
            if len(raw)>16*1024*1024:raise ValueError("mirror HTML exceeds 16 MiB")
            html=a.decode_html(raw,charset);root=a.DOM(html).root
            rel="html/"+hashlib.sha256(actual.encode()).hexdigest()+".html"
            (store.root/rel).parent.mkdir(parents=True,exist_ok=True);(store.root/rel).write_bytes(raw)
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)",(actual,source["key"],rel,a.now(),"ok",None,actual));store.db.commit()
            count=0;children=0
            for node in root.all({"a","area","iframe","frame"}):
                link=a.resolve(actual,node.attrs.get("href") or node.attrs.get("src", ""))
                if not link:continue
                original=clean(link);parts=urlsplit(original)
                binary=bool(a.EXT.search(parts.path))
                if binary or allowed(source,original):
                    store.db.execute("INSERT OR IGNORE INTO mirror_links VALUES(?,?,?,?,?,?,?)",(source["key"],actual,original,node.text(),"binary" if binary else "page",None,a.now()))
                if binary:count+=1;continue
                extension=Path(parts.path).suffix.lower()
                if allowed(source,original) and navigable(source,original) and extension in {"",".html",".htm",".php",".cgi",".asp",".aspx"}:
                    store.db.execute("INSERT OR IGNORE INTO mirror_frontier(site,url,mode) VALUES(?,?,?)",(source["key"],original,mode));children+=1
            store.db.commit()
            matched=match_records(store,source,html,actual,index) if count else 0
            if source["key"]=="moepedia" and re.fullmatch(r"/game/\d+/?",urlsplit(a.original(actual)).path):
                from portals import parse_moepedia
                for record in parse_moepedia(html,actual):store.add(record);matched+=1
            if matched and source["key"]=="moepedia":
                for record in parse_moepedia(html,actual):
                    for link in record["mirrors"]:
                        store.db.execute("UPDATE mirror_links SET matched_item=? WHERE site=? AND parent_url=? AND url=?",(record["id"],source["key"],actual,clean(link)))
            if source["key"]=="multi_inside":
                from portals import parse_multi_inside
                for record in parse_multi_inside(html,actual):
                    store.add(record);matched+=1
                    for link in record["mirrors"]:
                        store.db.execute("UPDATE mirror_links SET matched_item=? WHERE site=? AND parent_url=? AND url=?",(record["id"],source["key"],actual,clean(link)))
            found+=matched
            status="review" if count and not matched else "captured"
            if not count and not children:status="review"
            store.db.execute("UPDATE mirror_frontier SET status=?,capture_url=?,path=?,checked_at=?,error=? WHERE site=? AND url=? AND mode=?",(status,actual,rel,a.now(),"unmatched/unsupported catalogue page" if status=="review" else None,source["key"],url,mode));store.db.commit()
            print("mirror",source["key"],status,"files",matched,actual,flush=True)
        except (OSError,ValueError,http.client.HTTPException) as error:
            store.db.execute("UPDATE mirror_frontier SET status='failed',capture_url=?,path=?,checked_at=?,error=? WHERE site=? AND url=? AND mode=?",(actual,rel,a.now(),str(error),source["key"],url,mode));store.db.commit();print("mirror failed",source["key"],url,error,flush=True)
    store.export();return processed,found


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default="archive");parser.add_argument("--site",choices=["all",*definitions()],default="all")
    parser.add_argument("--mode",choices=["auto","live","archive"],default="auto");parser.add_argument("--max-pages",type=a.positive,default=100)
    args=parser.parse_args();store=a.Store(args.root)
    try:
        initialize(store)
        for key,source in definitions().items():
            if args.site=="all" and source["adapter"]=="native":continue
            if args.site in {"all",key}:print("mirror coverage",key,crawl(store,source,a.Fetcher(delay=10,timeout=25,retries=2),("live" if source["adapter"]=="platform" else "archive") if args.mode=="auto" else args.mode,args.max_pages),flush=True)
    finally:store.db.close()
