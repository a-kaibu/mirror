"""Find missing Holyseal product captures using catalog IDs and actual archive dates."""
import argparse
import json
import http.client
import re
from urllib.parse import urlencode,urlsplit,parse_qs
import archiver as a


def initialize(store):
    from erogamescape import initialize as initialize_metadata
    initialize_metadata(store)
    store.db.execute("""CREATE TABLE IF NOT EXISTS holy_inventory(product_code INTEGER PRIMARY KEY,
        status TEXT NOT NULL,requested_url TEXT,chosen_url TEXT,checked_at TEXT NOT NULL,error TEXT)""")
    store.db.commit()


def parsed_codes(store):
    codes=set()
    for row in store.db.execute("SELECT data FROM items WHERE site='holyseal'").fetchall():
        url=json.loads(row[0]).get("original_page_url","")
        value=parse_qs(urlsplit(url).query).get("prdcode",[""])[0]
        if value.isdigit():codes.add(int(value))
    return codes


def capture_url(payload, code):
    if not isinstance(payload,dict):raise ValueError("availability response is not an object")
    snapshots=payload.get("archived_snapshots") or {}
    if not isinstance(snapshots,dict):raise ValueError("invalid snapshot data")
    closest=snapshots.get("closest") or {}
    if not isinstance(closest,dict):raise ValueError("invalid closest snapshot")
    if not closest:return None
    if closest.get("available") is not True or str(closest.get("status"))!="200":return None
    url=closest.get("url","");match=a.WAYBACK.match(url)
    if not match or not re.fullmatch(r"\d{14}",match[1]):raise ValueError("invalid archived page URL")
    source=urlsplit(a.original(url))
    found=parse_qs(source.query).get("prdcode",[""])[0]
    if (source.scheme not in {"http","https"} or source.hostname not in {"holyseal.net","www.holyseal.net"}
            or source.path!="/cgi-bin/mlistview.cgi" or not found.isdigit() or int(found)!=code):
        raise ValueError("availability response belongs to another product")
    return a.canonical(url)


def collect(store, fetcher, limit=None, retry=False):
    initialize(store)
    known=parsed_codes(store)
    values={json.loads(r[0]).get("holyseal_id") for r in store.db.execute("SELECT data FROM external_metadata WHERE provider='erogamescape' AND entity='holyseal'").fetchall()}
    codes=sorted((code for code in values if type(code) is int and code>0),reverse=True)
    checked=found=0
    for code in codes:
        if code in known:continue
        prior=store.db.execute("SELECT status FROM holy_inventory WHERE product_code=?",(code,)).fetchone()
        if prior and not (retry and prior[0]!="parsed"):continue
        if limit is not None and checked>=limit:break
        checked+=1;requested=chosen=None
        try:
            for host in ["holyseal.net","www.holyseal.net"]:
                requested=f"http://{host}/cgi-bin/mlistview.cgi?prdcode={code}"
                endpoint="https://archive.org/wayback/available?"+urlencode({"url":requested})
                with fetcher.open(endpoint) as response:
                    raw=response.read(2*1024*1024+1)
                if len(raw)>2*1024*1024:raise ValueError("availability response exceeds bound")
                chosen=capture_url(json.loads(raw),code)
                if chosen:break
            status="no_capture_returned"
            error=None
            if chosen:
                failures=a.crawl(store,fetcher,"holyseal",chosen,max_items=1,export=False)
                status="failed" if failures else "parsed"
                error="saved capture needs parser/network review" if failures else None
                if not failures:found+=1;known.add(code)
            store.db.execute("INSERT OR REPLACE INTO holy_inventory VALUES(?,?,?,?,?,?)",(code,status,requested,chosen,a.now(),error));store.db.commit()
        except (OSError,ValueError,http.client.HTTPException) as error:
            store.db.execute("INSERT OR REPLACE INTO holy_inventory VALUES(?,?,?,?,?,?)",(code,"failed",requested,chosen,a.now(),str(error)));store.db.commit()
            status="failed"
        print("Holyseal inventory",code,status,chosen,flush=True)
        if checked%50==0:
            store.export();known.update(parsed_codes(store))
    store.export();return checked,found


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default="archive");parser.add_argument("--limit",type=a.positive)
    parser.add_argument("--retry",action="store_true");args=parser.parse_args()
    store=a.Store(args.root)
    try:print("Holyseal inventory coverage",collect(store,a.Fetcher(delay=10,timeout=30,retries=2),args.limit,args.retry),flush=True)
    finally:store.db.close()
