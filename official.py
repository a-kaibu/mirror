"""Collect publisher-hosted distributions using ErogameScape catalog seeds."""
import argparse
from collections import deque
import hashlib
import http.client
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, unquote
import archiver as a
from catalog import release_info, normalized, KINDS
import erogamescape as e

BINARY = re.compile(r"\.(?:zip|exe|7z|rar|lzh|cab|msi|xp3|mp4|wmv|mpg|mpeg)(?:$|[?#])",re.I)
NAVIGATION = re.compile(r"download|trial|taiken|special|support|patch|体験|ダウンロード|修正|アップデート",re.I)


def initialize(store):
    e.initialize(store)
    store.db.execute("""CREATE TABLE IF NOT EXISTS official_scans(game_id INTEGER PRIMARY KEY,
        seed_url TEXT,status TEXT NOT NULL,found INTEGER NOT NULL DEFAULT 0,error TEXT,updated_at TEXT NOT NULL)""")
    store.db.commit()


def binary_links(node, page):
    result=[]
    for link in node.all({"a"}):
        target=a.resolve(page,link.attrs.get("href",""))
        if target and BINARY.search(urlsplit(target).path):result.append((link,target))
    return result


def parse(html, page, game, brand, scoped=True, hint=None):
    root=a.DOM(html).root
    records={}
    unsupported=[]
    filenames={unquote(urlsplit(u).path.rsplit("/",1)[-1]) for _,u in binary_links(root,page)}
    for link,target in binary_links(root,page):
        filename=unquote(urlsplit(target).path.rsplit("/",1)[-1])
        def describe(n):
            marker=" ".join([n.attrs.get("id",""),n.attrs.get("class","")])
            trial=" 体験版" if re.search(r"(?<![a-z])trial(?![a-z])",marker,re.I) else ""
            return " ".join([n.text(),n.attrs.get("title","")]+[img.attrs.get("alt","") for img in n.all({"img"})])+trial
        context=describe(link);node=link.parent
        while node is not None and node.tag not in {"body","html","root"}:
            text=describe(node)
            names={unquote(urlsplit(u).path.rsplit("/",1)[-1]) for _,u in binary_links(node,page)}
            if len(text)>6000 or len(names)>1:break
            context=text
            if re.search(r"体験|trial|taiken|パッチ|patch|修正|アップデート|update|ムービー|movie|動画|(?:^|\W)PV(?:$|\W)",text,re.I):break
            node=node.parent
        label=context+" "+filename
        kind=release_info({"title":label,"filename":filename})[0]
        if kind=="other" and re.search(r"フライヤー|チラシ|バナー|壁紙|アイコン|ポスター|説明書|セーブ|banner|wallpaper|flyer|manual",label,re.I):
            continue  # Publisher scan targets trials, patches and movies.
        if kind=="other" and hint in KINDS and hint!="other" and len(filenames)==1:
            kind=hint
        if kind=="other" or (not scoped and normalized(game["gamename"]) not in normalized(context)):
            unsupported.append(target);continue
        key=hashlib.sha256(("official\n"+page+"\n"+filename).encode()).hexdigest()
        if key not in records:
            md5s=re.findall(r"MD5\s*[:：]?\s*([a-f0-9]{32})",context,re.I)
            exact=re.findall(r"([0-9][0-9,]*)\s*bytes",context,re.I)
            version=re.search(r"(?:ver(?:sion)?[._ -]*|v)(\d+(?:\.\d+)*)",filename,re.I)
            records[key]={"id":key,"site":"official","page_url":page,"original_page_url":page,
                "title":game["gamename"],"brand":brand["brandname"],"release_name":KINDS[kind]+(" ver"+version[1] if version else ""),
                "filename":filename,"mirrors":[],"expected_md5":md5s[0].lower() if len(set(md5s))==1 else None,
                "expected_size":int(exact[0].replace(",","")) if len(set(exact))==1 else None,
                "size_text":None,"published_at":None,"fields":{"erogamescape_game_id":game["id"],"erogamescape_brand_id":brand["id"],"official_page":page}}
        records[key]["mirrors"].append(target)
    for record in records.values():record["mirrors"]=list(dict.fromkeys(record["mirrors"]))
    return list(records.values()),unsupported


def seeds(store):
    brands={row[0]:json.loads(row[1]) for row in store.db.execute("SELECT external_id,data FROM external_metadata WHERE provider='erogamescape' AND entity='brand'")}
    eligible={row[0] for row in store.db.execute("SELECT external_id FROM metadata_links WHERE entity='brand'")}
    for row in store.db.execute("SELECT data FROM external_metadata WHERE provider='erogamescape' AND entity='game' ORDER BY CASE WHEN length(coalesce(json_extract(data,'$.trial_url'),''))>0 THEN 0 ELSE 1 END,external_id DESC").fetchall():
        game=json.loads(row[0]);brand=brands.get(game["brandname"])
        if not brand or brand["id"] not in eligible:continue
        home=game.get("shoukai") or brand.get("url")
        if not home:continue
        allowed={urlsplit(u).hostname for u in [home,brand.get("url") or home]}
        trial=game.get("trial_url")
        # trial_url may be a third-party mirror; do not label that publisher-hosted.
        seed=trial if trial and urlsplit(trial).hostname in allowed else game.get("shoukai")
        if not seed or urlsplit(seed).scheme not in {"http","https"}:continue
        yield game,brand,a.canonical(seed)


def scan(store, fetcher, game, brand, seed, max_pages=20):
    queue=deque([(seed,0)]);seen=set();found=0;failures=[];unclassified=[]
    hints={seed: {"trial"}} if game.get("trial_url") and a.canonical(game["trial_url"])==seed else {}
    while queue and len(seen)<max_pages:
        url,depth=queue.popleft()
        if url in seen:continue
        seen.add(url)
        rel="html/"+hashlib.sha256(url.encode()).hexdigest()+".html"
        try:
            with fetcher.open(url) as response:
                raw=response.read(16*1024*1024+1);actual=a.canonical(response.geturl())
                charset=response.headers.get_content_charset()
            if len(raw)>16*1024*1024:raise ValueError("publisher HTML exceeds 16 MiB")
            (store.root/rel).parent.mkdir(parents=True,exist_ok=True);(store.root/rel).write_bytes(raw)
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)",(url,"official",rel,a.now(),"ok",None,actual));store.db.commit()
            html=a.decode_html(raw,charset)
            base=urlsplit(actual);folder=base.path.rsplit("/",1)[0]+"/"
            home_path=urlsplit(game.get("shoukai") or "").path
            product_folder=home_path if home_path.endswith("/") else home_path.rsplit("/",1)[0]+"/"
            scoped=product_folder not in {"","/"} and base.path.startswith(product_folder)
            options=hints.get(url,set())
            hint=next(iter(options)) if len(options)==1 else None
            records,unknown=parse(html,actual,game,brand,scoped=scoped,hint=hint)
            unclassified.extend(unknown)
            for record in records:
                from mirror_sources import add_record
                add_record(store,record)
            found+=len(records)
            if depth<2:
                for link in a.DOM(html).root.all({"a","iframe","frame"}):
                    target=a.resolve(actual,link.attrs.get("href") or link.attrs.get("src", ""))
                    if not target or BINARY.search(urlsplit(target).path):continue
                    parts=urlsplit(target)
                    extension=Path(parts.path).suffix.lower()
                    if extension not in {"",".html",".htm",".php",".cgi",".asp",".aspx"}:continue
                    if parts.hostname==base.hostname and parts.path.startswith(folder) and (link.tag in {"iframe","frame"} or NAVIGATION.search(link.text()+" "+parts.path)):
                        kind=release_info({"title":link.text()+" "+parts.path.rsplit("/",1)[-1],"filename":""})[0]
                        if kind!="other":hints.setdefault(target,set()).add(kind)
                        queue.append((target,depth+1))
        except (OSError,ValueError,http.client.HTTPException) as error:
            failures.append({"url":url,"error":str(error)})
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)",(url,"official",rel if (store.root/rel).exists() else None,a.now(),"failed",str(error),None));store.db.commit()
    if queue:failures.append({"error":"page bound reached; needs continuation","remaining":len(queue)})
    if unclassified:failures.append({"error":"unclassified binary links need review","urls":list(dict.fromkeys(unclassified))})
    status="review" if failures else ("found" if found else "no_direct_links")
    store.db.execute("INSERT OR REPLACE INTO official_scans VALUES(?,?,?,?,?,?)",(game["id"],seed,status,found,json.dumps(failures,ensure_ascii=False) if failures else None,a.now()));store.db.commit()
    print("official",game["id"],brand["brandname"],game["gamename"],status,found,flush=True)
    return found


def collect(store, fetcher, limit=None, retry=False):
    initialize(store);done=0
    for game,brand,seed in seeds(store):
        existing=store.db.execute("SELECT status FROM official_scans WHERE game_id=?",(game["id"],)).fetchone()
        if existing and not (retry and existing[0]=="review"):continue
        if limit is not None and done>=limit:break
        scan(store,fetcher,game,brand,seed);done+=1
    store.export();return done


class HostFetcher:
    """Independent publishers progress concurrently; each host retains pacing."""
    def __init__(self):
        import threading
        self.guard=threading.Lock();self.hosts={}

    def open(self,url,headers=None):
        import threading
        key=urlsplit(url).hostname
        with self.guard:
            if key not in self.hosts:self.hosts[key]=(threading.Lock(),a.Fetcher(delay=3,timeout=25,retries=2))
            lock,fetcher=self.hosts[key]
        with lock:return fetcher.open(url,headers)


def collect_parallel(root,workers=4,limit=None,retry=False):
    from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
    from archive_lock import exclusive_file
    store=a.Store(root);initialize(store);fetcher=HostFetcher();submitted=completed=0
    def collect_one(game,brand,seed):
        with exclusive_file(Path(root)/".claims"/("official-scan-"+str(game["id"])+".lock")):
            local=a.Store(root)
            try:
                existing=local.db.execute("SELECT status FROM official_scans WHERE game_id=?",(game["id"],)).fetchone()
                if existing and not (retry and existing[0]=="review"):return False
                scan(local,fetcher,game,brand,seed);return True
            finally:local.db.close()
    try:
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="publisher") as pool:
            jobs=set()
            for game,brand,seed in seeds(store):
                existing=store.db.execute("SELECT status FROM official_scans WHERE game_id=?",(game["id"],)).fetchone()
                if existing and not (retry and existing[0]=="review"):continue
                if limit is not None and submitted>=limit:break
                jobs.add(pool.submit(collect_one,game,brand,seed));submitted+=1
                if len(jobs)>=workers:
                    done,jobs=wait(jobs,return_when=FIRST_COMPLETED)
                    completed+=sum(int(job.result()) for job in done)
            for job in jobs:completed+=int(job.result())
        store.export();return completed
    finally:store.db.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default="archive");parser.add_argument("--limit",type=a.positive)
    parser.add_argument("--retry",action="store_true");parser.add_argument("--workers",type=a.positive,default=4);args=parser.parse_args()
    if args.workers>1:
        print("official scanned",collect_parallel(args.root,args.workers,args.limit,args.retry),flush=True)
    else:
        store=a.Store(args.root)
        try:print("official scanned",collect(store,a.Fetcher(delay=3,timeout=25,retries=2),args.limit,args.retry),flush=True)
        finally:store.db.close()
