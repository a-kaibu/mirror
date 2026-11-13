"""Discover binary captures at their own dates, retaining index evidence in SQLite."""
import argparse
import json
import re
from urllib.parse import urlencode, urlsplit, parse_qs
import archiver as a


def index_identity(url):
    parts=urlsplit(url)
    if parts.scheme=='ftp':
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError('not an anonymous public FTP source')
        return 'ftp',parts.hostname,21 if parts.port is None else parts.port,parts.path,parts.query
    return ('web',)+a.archive_url_identity(url)


def captures(payload, target, scheme="https"):
    if not isinstance(payload, list):
        raise ValueError("CDX response is not an array")
    if not payload:
        return []
    required = {"timestamp", "original", "statuscode", "digest"}
    if not isinstance(payload[0], list) or not required <= set(payload[0]):
        raise ValueError("CDX columns missing")
    base = index_identity(target)
    result = []
    for values in payload[1:]:
        if len(values) != len(payload[0]):
            raise ValueError("CDX row has unexpected columns")
        row = dict(zip(payload[0], values))
        # CDX may canonicalize HTTP(S) and explicit default ports. Preserve
        # nondefault ports, path and query so another service cannot match.
        try:identity=index_identity(row["original"])
        except ValueError:continue
        if identity!=base:continue
        success="226" if base[0]=="ftp" else "200"
        if str(row["statuscode"]) != success or not re.fullmatch(r"\d{14}", row["timestamp"]):
            continue
        if row.get("mimetype", "").lower() in {"text/html", "application/xhtml+xml"}:
            continue
        result.append({"url": a.replay(row["original"], row["timestamp"], raw=True, scheme=scheme),
            "original_url": row["original"], "captured_at": row["timestamp"], "index_digest": row["digest"]})
    return result


def discover(store, fetcher, limit=None, refresh=False, site="both"):
    attempted = found = 0
    targets={}
    for item in store.rows(site):
        if item['status']!='failed':continue
        record=json.loads(item['data'])
        urls=record['mirrors']+record.get('fields',{}).get('recovery_urls',[])
        # Legacy parsers retain the exact FTP href as provenance. Query its
        # own archived resource, never invent an HTTP service or change ports.
        for link in record.get('fields',{}).get('published_ftp_links',[]):
            target=link.get('url') if isinstance(link,dict) else None
            if not isinstance(target,str):continue
            try:
                parts=urlsplit(target)
                if parts.scheme!='ftp':continue
                index_identity(target)
            except ValueError:continue
            if a.EXT.search(parts.path):urls=urls+[target]
        originals=dict.fromkeys(a.original(u) for u in urls)
        for target in originals:targets.setdefault(target,[]).append((item['id'],record['filename']))
    lookups={row['url']:row for row in store.db.execute('SELECT url,status,queried_at FROM wayback_lookups')}
    def priority(target):
        saved=lookups.get(target)
        # Apply successful cached evidence first without spending the network
        # budget. Then query unseen URLs and the least recently tried URLs.
        cached=bool(saved and saved['status']=='ok' and not refresh)
        return (0 if cached else 1,saved['queried_at'] or '' if saved else '',target)
    for target in sorted(targets,key=priority):
        scheme=a.archive_index_scheme(store,target)
        saved = store.db.execute("SELECT * FROM wayback_lookups WHERE url=?", (target,)).fetchone()
        ftp_target=urlsplit(target).scheme=='ftp'
        if ftp_target and saved and saved['status']=='ok':
            transport=store.db.execute('SELECT index_url FROM archive_index_transports WHERE target_url=?',(target,)).fetchone()
            # A legacy HTTP-200 filter cannot prove an FTP-226 result is absent.
            if not transport or 'statuscode:226' not in parse_qs(urlsplit(transport[0]).query).get('filter',[]):
                saved=None
        if saved and saved["status"] == "ok" and not refresh:
            payload = json.loads(saved["data"])
        else:
            if limit is not None and attempted >= limit:
                return attempted, found
            attempted += 1
            endpoint = "https://web.archive.org/cdx/search/cdx?" + urlencode({"url": target,
                "output": "json", "filter": "statuscode:226" if ftp_target else "statuscode:200", "fl": "timestamp,original,mimetype,statuscode,digest,length", "limit": "10000"})
            try:
                with a.open_archive_index(fetcher,endpoint) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                    index_url = response.geturl()
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("CDX response exceeds 16 MiB")
                payload = json.loads(raw)
                if len(payload) >= 10001:
                    raise ValueError("CDX result truncated; requires pagination")
                captures(payload, target)  # Validate before recording success.
                scheme=a.archive_index_scheme(store,target,index_url)
                store.db.execute("INSERT OR REPLACE INTO wayback_lookups VALUES(?,?,?,?,?)", (target, a.now(), "ok", json.dumps(payload), None))
            except (OSError, ValueError) as error:
                store.db.execute("INSERT OR REPLACE INTO wayback_lookups VALUES(?,?,?,?,?)", (target, a.now(), "failed", None, str(error)))
                store.db.commit()
                print("CDX failed", target, error, flush=True)
                continue
        candidates = captures(payload, target, scheme=scheme)
        for ident,filename in targets[target]:
            for candidate in candidates:
                store.db.execute("INSERT OR IGNORE INTO wayback_candidates VALUES(?,?,?,?,?)", (ident, candidate["url"], candidate["original_url"], candidate["captured_at"], candidate["index_digest"]))
            store.db.execute("""UPDATE items SET status='pending' WHERE id=? AND status='failed'
                AND EXISTS(SELECT 1 FROM wayback_candidates c WHERE c.item_id=items.id
                  AND NOT EXISTS(SELECT 1 FROM attempts t WHERE t.item_id=items.id AND t.url=c.url))""",(ident,))
            store.db.commit()
            found += len(candidates)
            print("CDX", filename, target, len(candidates), "capture(s)", flush=True)
    return attempted, found


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="archive")
    parser.add_argument("--limit", type=a.positive)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--site", default="both", help="source key, or both for all registered records")
    args = parser.parse_args()
    store = a.Store(args.root)
    try:
        print("CDX discovery:", discover(store, a.Fetcher(delay=10, timeout=30, retries=2), args.limit, args.refresh, args.site))
    finally:
        store.db.close()
