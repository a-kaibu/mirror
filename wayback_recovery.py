"""Discover binary captures at their own dates, retaining index evidence in SQLite."""
import argparse
import json
import re
from urllib.parse import urlencode, urlsplit
import archiver as a


def captures(payload, target):
    if not isinstance(payload, list):
        raise ValueError("CDX response is not an array")
    if not payload:
        return []
    required = {"timestamp", "original", "statuscode", "digest"}
    if not isinstance(payload[0], list) or not required <= set(payload[0]):
        raise ValueError("CDX columns missing")
    base = urlsplit(target)
    result = []
    for values in payload[1:]:
        if len(values) != len(payload[0]):
            raise ValueError("CDX row has unexpected columns")
        row = dict(zip(payload[0], values))
        url = urlsplit(row["original"])
        # CDX may canonicalize http/https. Never accept another path or host.
        if (url.scheme not in {"http", "https"} or url.netloc.lower() != base.netloc.lower()
                or url.path != base.path or url.query != base.query):
            continue
        if str(row["statuscode"]) != "200" or not re.fullmatch(r"\d{14}", row["timestamp"]):
            continue
        if row.get("mimetype", "").lower() in {"text/html", "application/xhtml+xml"}:
            continue
        result.append({"url": a.replay(row["original"], row["timestamp"], raw=True),
            "original_url": row["original"], "captured_at": row["timestamp"], "index_digest": row["digest"]})
    return result


def discover(store, fetcher, limit=None, refresh=False, site="both"):
    attempted = found = 0
    for item in store.rows(site):
        if item["status"] != "failed":
            continue
        record = json.loads(item["data"])
        originals = list(dict.fromkeys(a.original(u) for u in record["mirrors"] + record.get("fields",{}).get("recovery_urls",[])))
        for target in originals:
            saved = store.db.execute("SELECT * FROM wayback_lookups WHERE url=?", (target,)).fetchone()
            if saved and saved["status"] == "ok" and not refresh:
                payload = json.loads(saved["data"])
            else:
                if limit is not None and attempted >= limit:
                    return attempted, found
                attempted += 1
                endpoint = "https://web.archive.org/cdx/search/cdx?" + urlencode({"url": target,
                    "output": "json", "filter": "statuscode:200", "fl": "timestamp,original,mimetype,statuscode,digest,length", "limit": "10000"})
                try:
                    with a.open_archive_index(fetcher,endpoint) as response:
                        raw = response.read(16 * 1024 * 1024 + 1)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError("CDX response exceeds 16 MiB")
                    payload = json.loads(raw)
                    if len(payload) >= 10001:
                        raise ValueError("CDX result truncated; requires pagination")
                    captures(payload, target)  # Validate before recording success.
                    store.db.execute("INSERT OR REPLACE INTO wayback_lookups VALUES(?,?,?,?,?)", (target, a.now(), "ok", json.dumps(payload), None))
                except (OSError, ValueError) as error:
                    store.db.execute("INSERT OR REPLACE INTO wayback_lookups VALUES(?,?,?,?,?)", (target, a.now(), "failed", None, str(error)))
                    store.db.commit()
                    print("CDX failed", target, error, flush=True)
                    continue
            candidates = captures(payload, target)
            for candidate in candidates:
                store.db.execute("INSERT OR IGNORE INTO wayback_candidates VALUES(?,?,?,?,?)", (item["id"], candidate["url"], candidate["original_url"], candidate["captured_at"], candidate["index_digest"]))
            store.db.commit()
            found += len(candidates)
            print("CDX", record["filename"], target, len(candidates), "capture(s)", flush=True)
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
