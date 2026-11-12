"""Collect AliceSoft official support sections without replacing product scans."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import archiver as a
import official as o
import mirror_sources as m

# These are publisher pages linked by the Rance guide, not guessed binary URLs.
PAGES = [
    (20764, "ランス10", "https://www.alicesoft.com/support/rance10.html"),
    (22060, "ランス03 リーザス陥落", "https://www.alicesoft.com/support/rance03.html"),
    (8134, "戦国ランス", "https://www.alicesoft.com/support/rance7.html"),
    (6582, "GALZOOアイランド", "https://www.alicesoft.com/support/galzoo.html"),
    (6582, "GALZOOアイランド", "https://www.alicesoft.com/galzoo/hard.html"),
]


def sections(root):
    """Each sibling h4 starts its own version; following requirements stay local."""
    for container in root.all({"div"}):
        if "cont01" not in container.attrs.get("class", "").split():
            continue
        current = None
        for child in container.children:
            if isinstance(child, a.Node) and child.tag == "h4":
                if current is not None:
                    yield current
                current = a.Node("section")
            if current is not None:
                # Read-only view: never re-parent the original DOM nodes.
                current.children.append(child)
        if current is not None:
            yield current


def parse(html, page, game, brand):
    root = a.DOM(html).root
    records = {}
    chunks = list(sections(root))
    if not chunks and urlsplit(page).path == "/galzoo/hard.html":
        chunks = [root]
    for section in chunks:
        text = " ".join(section.text().split())
        heading = next(iter(section.all({"h4"})), None)
        label = heading.text() if heading else "ハードモード追加パッチ"
        if not re.search("アップデータ|アップデート|修正|パッチ", label):
            continue
        version = re.search(r"(?:Ver\.?|バージョン)\s*(\d+(?:\.\d+)+)", label, re.I)
        links = o.binary_links(section, page)
        # Only infer a section's checksum when it names exactly one binary.
        filenames = {urlsplit(u).path.rsplit("/", 1)[-1] for _, u in links}
        checksums = set(re.findall(r"MD5\s*[:：]?\s*([a-f0-9]{32})", text, re.I))
        for link, target in links:
            if urlsplit(target).hostname != "download.alicesoft.com":
                continue
            from urllib.parse import unquote
            filename = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
            ident = hashlib.sha256(("official\n"+page+"\n"+filename).encode()).hexdigest()
            fields = {"erogamescape_game_id": game["id"], "erogamescape_brand_id": brand["id"],
                      "official_page": page, "support_heading": label,
                      "適用条件": text, "distribution_kind": "patch"}
            if version:
                fields["バージョン"] = version[1]
            if ident not in records:
                records[ident] = {"id": ident, "site": "official", "page_url": page,
                    "original_page_url": page, "title": game["gamename"], "brand": brand["brandname"],
                    "release_name": "パッチ ver"+version[1] if version else "パッチ ハードモード追加",
                    "filename": filename, "mirrors": [], "expected_md5": next(iter(checksums)).lower()
                    if len(checksums) == 1 and len(filenames) == 1 else None,
                    "expected_size": None, "size_text": link.text(), "published_at": None, "fields": fields}
            records[ident]["mirrors"].append(target)
    for record in records.values():
        record["mirrors"] = list(dict.fromkeys(record["mirrors"]))
    return list(records.values())


def collect(store, fetcher):
    o.initialize(store)
    store.db.execute("""CREATE TABLE IF NOT EXISTS official_support_scans(
        url TEXT PRIMARY KEY,game_id INTEGER,status TEXT,found INTEGER,error TEXT,checked_at TEXT)""")
    store.db.commit()
    registered = []
    for game_id, title, page in PAGES:
        count = 0
        try:
            row = store.db.execute("SELECT data FROM external_metadata WHERE provider='erogamescape' AND entity='game' AND external_id=?", (game_id,)).fetchone()
            if not row:
                raise ValueError("ErogameScape game metadata not yet available")
            game = json.loads(row[0])
            row = store.db.execute("SELECT data FROM external_metadata WHERE provider='erogamescape' AND entity='brand' AND external_id=?", (game["brandname"],)).fetchone()
            if not row:
                raise ValueError("ErogameScape brand metadata not yet available")
            brand = json.loads(row[0])
            if game["gamename"] != title:
                raise ValueError("catalogue identity changed; requires review")
            with fetcher.open(page) as response:
                raw = response.read(4*1024*1024+1)
                actual = a.canonical(response.geturl())
                charset = response.headers.get_content_charset()
            if len(raw) > 4*1024*1024 or actual != page:
                raise ValueError("support page changed location or exceeds bound")
            rel = "html/"+hashlib.sha256(page.encode()).hexdigest()+".html"
            (store.root/rel).parent.mkdir(parents=True, exist_ok=True)
            (store.root/rel).write_bytes(raw)
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,response_url) VALUES(?,?,?,?,?,?)", (page,"official",rel,a.now(),"ok",actual))
            store.db.commit()
            records = parse(a.decode_html(raw, charset), page, game, brand)
            if not records:
                raise ValueError("no supported update sections; requires review")
            for record in records:
                m.add_record(store, record)
                registered.append(record["id"])
                count += 1
                print("official support", title, record["release_name"], record["filename"], flush=True)
            status, error = "captured", None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            status, error = "review", str(exc)
            print("official support review", title, error, flush=True)
        store.db.execute("INSERT OR REPLACE INTO official_support_scans VALUES(?,?,?,?,?,?)", (page,game_id,status,count,error,a.now()))
        store.db.commit()
    store.export()
    return registered


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="archive")
    args = parser.parse_args()
    store = a.Store(args.root)
    try:
        print("support files", len(collect(store, a.Fetcher(delay=3, timeout=30, retries=2))))
    finally:
        store.db.close()
