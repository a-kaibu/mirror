"""SQLite catalog and static HTML generation. Source records retain provenance."""
from __future__ import annotations

import hashlib
from html import escape
import json
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata

KINDS = {"trial": "体験版", "patch": "パッチ", "full": "本編", "movie": "ムービー", "other": "その他"}
PRIORITY = {"trial": 0, "patch": 1, "full": 2, "movie": 3, "other": 4}
# Bump when release_info changes its classification rules.
CLASSIFICATION_REVISION = "movie-filename-1"


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def identity(*values):
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def release_info(record):
    label = record.get("release_name") or record["title"]
    if record.get("fields", {}).get("distribution_kind") == "full":
        return "full", record.get("fields", {}).get("version"), record.get("fields", {}).get("platform"), record.get("fields", {}).get("requirements")
    # A repaired trial remains a trial, not a patch for the commercial game.
    if re.search(r"体験|評価版|trial|(?:^|[ _])taiken", label + " " + (record["filename"] or ""), re.I):
        kind = "trial"
    elif re.search(r"ムービー|movie|動画|(?:^|[ _])PV(?:$|[ _])", label, re.I):
        # A corrected movie is still a movie, rather than a game patch.
        kind = "movie"
    elif re.search(r"パッチ|修正|アップデート|patch|update", label, re.I):
        kind = "patch"
    elif re.search(r"ムービー|movie|動画|PV|デモ", label, re.I):
        kind = "movie"
    elif re.search(r"\.(?:wmv|mp4|mpg|mpeg|avi|mov|flv|webm|ogv)$", record.get("filename") or "", re.I):
        # Old catalogues may publish only a game title and a direct media file.
        # Do not invent a release label or treat an image's byte count as size.
        kind = "movie"
    else:
        kind = "other"
    fields = record.get("fields", {})
    version = re.search(r"(?:ver(?:sion)?\.?\s*|v)(\d+(?:\.\d+)*[a-z]?)", label, re.I)
    return kind, fields.get("バージョン") or (version[1] if version else None), fields.get("対象OS"), fields.get("適用条件") or fields.get("備考") or None


def initialize(store):
    db = store.db
    db.executescript("""
      CREATE TABLE IF NOT EXISTS manufacturers(id TEXT PRIMARY KEY, name TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS manufacturer_aliases(alias TEXT PRIMARY KEY, manufacturer_id TEXT NOT NULL REFERENCES manufacturers(id));
      CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, manufacturer_id TEXT NOT NULL REFERENCES manufacturers(id), title TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS item_catalog(item_id TEXT PRIMARY KEY REFERENCES items(id), product_id TEXT NOT NULL REFERENCES products(id), kind TEXT NOT NULL, version TEXT, platform TEXT, requirements TEXT);
      CREATE TABLE IF NOT EXISTS wayback_lookups(url TEXT PRIMARY KEY,queried_at TEXT NOT NULL,status TEXT NOT NULL,data TEXT,error TEXT);
      CREATE TABLE IF NOT EXISTS wayback_candidates(item_id TEXT NOT NULL,url TEXT NOT NULL,original_url TEXT NOT NULL,captured_at TEXT NOT NULL,index_digest TEXT,PRIMARY KEY(item_id,url));
      CREATE TABLE IF NOT EXISTS published_objects(key TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL, verified_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS item_catalog_product ON item_catalog(product_id);
      CREATE INDEX IF NOT EXISTS products_manufacturer ON products(manufacturer_id);
      CREATE INDEX IF NOT EXISTS manufacturer_aliases_manufacturer ON manufacturer_aliases(manufacturer_id);
      CREATE TABLE IF NOT EXISTS catalog_classification_state(
        item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE, revision TEXT NOT NULL);
      CREATE TRIGGER IF NOT EXISTS catalog_classification_source_changed
        AFTER UPDATE OF data ON items WHEN OLD.data IS NOT NEW.data BEGIN
          DELETE FROM catalog_classification_state WHERE item_id=NEW.id;
        END;
      CREATE TRIGGER IF NOT EXISTS catalog_classification_inserted
        AFTER INSERT ON item_catalog BEGIN
          DELETE FROM catalog_classification_state WHERE item_id=NEW.item_id;
        END;
      CREATE TRIGGER IF NOT EXISTS catalog_classification_kind_changed
        AFTER UPDATE OF kind ON item_catalog WHEN OLD.kind IS NOT NEW.kind BEGIN
          DELETE FROM catalog_classification_state WHERE item_id=NEW.item_id;
        END;
    """)
    if "remote_verified_at" not in {r[1] for r in db.execute("PRAGMA table_info(items)")}:
        db.execute("ALTER TABLE items ADD COLUMN remote_verified_at TEXT")
    if "reused_from" not in {r[1] for r in db.execute("PRAGMA table_info(items)")}:
        db.execute("ALTER TABLE items ADD COLUMN reused_from TEXT")
    if "response_url" not in {r[1] for r in db.execute("PRAGMA table_info(pages)")}:
        db.execute("ALTER TABLE pages ADD COLUMN response_url TEXT")
    if "tls_pin" not in {r[1] for r in db.execute("PRAGMA table_info(attempts)")}:
        db.execute("ALTER TABLE attempts ADD COLUMN tls_pin TEXT")
    missing=[row[0] for row in db.execute("SELECT id FROM items WHERE id NOT IN (SELECT item_id FROM item_catalog)")]
    for ident in missing:
        row=db.execute("SELECT data FROM items WHERE id=?",(ident,)).fetchone()
        index_record(store,json.loads(row[0]))
    db.commit()
    # Old workers can still write the six-column table. Triggers invalidate
    # their rows without changing that schema; current writers mark their work.
    # Reconcile only stale rows, releasing each bounded reader before writes.
    last=None
    while True:
        after=" AND c.item_id>?" if last is not None else ""
        params=(CLASSIFICATION_REVISION,last) if last is not None else (CLASSIFICATION_REVISION,)
        rows=db.execute("""SELECT i.id,i.data,c.kind FROM item_catalog c
            JOIN items i ON i.id=c.item_id
            LEFT JOIN catalog_classification_state s ON s.item_id=c.item_id
            WHERE (s.revision IS NULL OR s.revision!=?)"""+after+
            " ORDER BY c.item_id LIMIT 128",params).fetchall()
        if not rows:break
        changes=[];classified=[]
        for row in rows:
            kind=release_info(json.loads(row[1]))[0]
            if kind!=row[2]:changes.append((kind,row[0],row[1]))
            classified.append((CLASSIFICATION_REVISION,row[0],row[1],kind))
        with db:
            db.executemany("""UPDATE item_catalog SET kind=? WHERE item_id=?
                AND EXISTS(SELECT 1 FROM items i WHERE i.id=item_catalog.item_id AND i.data=?)""",changes)
            # A concurrent source edit must stay stale, rather than permanently
            # accepting a classification computed from its previous payload.
            db.executemany("""INSERT OR REPLACE INTO catalog_classification_state
                SELECT i.id,? FROM items i JOIN item_catalog c ON c.item_id=i.id
                WHERE i.id=? AND i.data=? AND c.kind=?""",classified)
        last=rows[-1][0]


def index_record(store, record):
    db = store.db
    name = record.get("brand") or "ブランド未確認"
    alias = normalized(name)
    manufacturer = db.execute("SELECT manufacturer_id FROM manufacturer_aliases WHERE alias=?", (alias,)).fetchone()
    maker_id = manufacturer[0] if manufacturer else identity(alias)
    db.execute("INSERT OR IGNORE INTO manufacturers VALUES(?,?)", (maker_id, name))
    db.execute("INSERT OR IGNORE INTO manufacturer_aliases VALUES(?,?)", (alias, maker_id))
    title = record["title"]
    # Unknown makers must not merge equally named products across unrelated sites.
    scope = record.get("original_page_url", record["id"]) if not record.get("brand") else ""
    product_id = identity(maker_id, normalized(title), scope)
    db.execute("INSERT OR IGNORE INTO products VALUES(?,?,?)", (product_id, maker_id, title))
    db.execute("INSERT OR REPLACE INTO item_catalog VALUES(?,?,?,?,?,?)", (record["id"], product_id, *release_info(record)))
    db.execute("""INSERT OR REPLACE INTO catalog_classification_state
        SELECT id,? FROM items WHERE id=? AND data=?""",
        (CLASSIFICATION_REVISION,record["id"],json.dumps(record,ensure_ascii=False)))


def alias_maker(store, alias, name):
    """An explicit curator decision. Never fuzzy-merge unrelated brands."""
    with store.db:
        target = store.db.execute("SELECT manufacturer_id FROM manufacturer_aliases WHERE alias=?", (normalized(name),)).fetchone()
        target_id = target[0] if target else identity(normalized(name))
        store.db.execute("INSERT OR IGNORE INTO manufacturers VALUES(?,?)", (target_id, name))
        old = store.db.execute("SELECT manufacturer_id FROM manufacturer_aliases WHERE alias=?", (normalized(alias),)).fetchone()
        affected=[]
        if old and old[0] != target_id:
            affected=[r[0] for r in store.db.execute("SELECT c.item_id FROM item_catalog c JOIN products p ON p.id=c.product_id WHERE p.manufacturer_id=? ORDER BY c.item_id",(old[0],))]
            store.db.execute("UPDATE manufacturer_aliases SET manufacturer_id=? WHERE manufacturer_id=?", (target_id, old[0]))
        for value in [alias, name]:
            store.db.execute("INSERT OR REPLACE INTO manufacturer_aliases VALUES(?,?)", (normalized(value), target_id))
        # Finish the thin ID reader before writes, then load one affected payload.
        # A spelling review must not retain or rewrite every source catalogue row.
        for ident in affected:
            row=store.db.execute("SELECT data FROM items WHERE id=?",(ident,)).fetchone()
            if row:index_record(store,json.loads(row[0]))
        store.db.execute("DELETE FROM products WHERE id NOT IN (SELECT product_id FROM item_catalog)")
        store.db.execute("DELETE FROM manufacturers WHERE id NOT IN (SELECT manufacturer_id FROM products) AND id NOT IN (SELECT manufacturer_id FROM manufacturer_aliases)")


def manifest(store, *, public=False):
    from archiver import now, public_url
    db = store.db
    makers = [dict(r) for r in db.execute("SELECT * FROM manufacturers WHERE id IN (SELECT manufacturer_id FROM products WHERE id IN (SELECT product_id FROM item_catalog)) ORDER BY name,id")]
    products = [dict(r) for r in db.execute("SELECT * FROM products WHERE id IN (SELECT product_id FROM item_catalog) ORDER BY title,id")]
    if not public and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_links'").fetchone():
        for entity, entries in [("brand", makers), ("game", products)]:
            linked = {}
            for row in db.execute("""SELECT l.local_id,l.external_id,l.method,m.data,m.fetched_at
                    FROM metadata_links l JOIN external_metadata m ON m.entity=l.entity AND m.external_id=l.external_id
                    WHERE l.entity=? AND m.provider='erogamescape' ORDER BY l.external_id""", (entity,)):
                linked.setdefault(row[0], []).append({"id": row[1], "match_method": row[2],
                    "fetched_at": row[4], "data": json.loads(row[3])})
            for entry in entries:
                if entry["id"] in linked:
                    entry["erogamescape"] = linked[entry["id"]]
    files = {}
    for row in db.execute("SELECT i.*,c.product_id,c.kind,c.version,c.platform,c.requirements FROM items i JOIN item_catalog c ON c.item_id=i.id ORDER BY i.id"):
        r = dict(row)
        data = json.loads(r.pop("data"))
        # Bytes are deduplicated globally in HF; each product keeps its association.
        file_id = identity(r["product_id"], r["sha256"] or r["id"])
        item = files.setdefault(file_id, {"id": file_id, "product_id": r["product_id"], "kind": r["kind"],
            "name": data.get("release_name") or data["title"], "version": r["version"], "platform": r["platform"],
            "requirements": r["requirements"], "filename": data["filename"], "size": r["size"], "md5": r["md5"],
            "sha256": r["sha256"], "download_url": None, "remote_key": None, "public_verified_at": None, "sources": []})
        if (r["status"] in {"verified", "archived"} and r["sha256"] and r["remote_sha256"] == r["sha256"] and r["remote_verified_at"]
                and not data.get("fields", {}).get("source_association_quarantined")):
            namespace, bucket, key = r["remote_key"].split("/", 2)
            item.update(download_url=public_url(namespace, bucket, key, download=True), remote_key=r["remote_key"], public_verified_at=r["remote_verified_at"])
        if not item["filename"] and data["filename"]:
            item["filename"] = data["filename"]
        source = {"id": r["id"], "site": r["site"], "page_url": data["page_url"],
            "release_name": data.get("release_name"), "kind": r["kind"], "version": r["version"],
            "platform": r["platform"], "requirements": r["requirements"],
            "reused_from": r["reused_from"],
            "original_page_url": data.get("original_page_url"), "filename": data["filename"],
            "published_at": data.get("published_at"), "mirrors": data["mirrors"], "expected_md5": data.get("expected_md5"),
            "expected_sha256": data.get("expected_sha256"), "expected_size": data.get("expected_size"), "size_text": data.get("size_text"),
            "status": r["status"], "fetched_at": r["fetched_at"], "source_url": r["source_url"], "error": r["error"]}
        if not public:
            source.update(fields=data.get("fields", {}), attempts=[dict(a) for a in db.execute(
                "SELECT url,attempted_at,error,tls_pin FROM attempts WHERE item_id=? ORDER BY id", (r["id"],))])
        item["sources"].append(source)
    return {"schema_version": 1, "generated_at": now(), "manufacturers": makers, "products": products,
        "files": sorted(files.values(), key=lambda f: (PRIORITY[f["kind"]], f["product_id"], f["id"])),
        "pages": [] if public else [dict(r) for r in db.execute("SELECT * FROM pages ORDER BY url")]}


def export_manifest(store, *, public=False):
    # Keep related catalog tables in one read snapshot while the crawler adds
    # new rows. WAL lets the producer continue without waiting for JSON output.
    store.db.execute("BEGIN")
    try:
        data = manifest(store, public=public)
    finally:
        store.db.rollback()
    path = store.root / "manifest.json"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=store.root, prefix="manifest-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=None if public else 2, separators=(",", ":") if public else None)
        stream.write("\n")
    temporary.replace(path)
    return data


def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1000 or unit == "PB":
            text = str(int(value)) if unit == "B" else f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        value /= 1000


def build_site(store, output):
    output = Path(output)
    if output.resolve() == store.root.resolve() or store.root.resolve().is_relative_to(output.resolve()):
        raise ValueError("site output must be separate from archive storage")
    data = export_manifest(store, public=True)
    output.mkdir(parents=True, exist_ok=True)
    for path in (Path(__file__).parent / "web").iterdir():
        if path.is_file():
            shutil.copyfile(path, output / path.name)
    shutil.copyfile(store.root / "manifest.json", output / "manifest.json")
    (output / ".nojekyll").touch()
    (output / "search").mkdir(exist_ok=True)
    (output / "search" / "index.html").write_text('''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JSON search</title><link rel="icon" href="../favicon.svg" type="image/svg+xml">
<script type="module" src="../search-page.mjs"></script></head><body><pre id="result" style="white-space:pre-wrap;overflow-wrap:anywhere">読み込み中</pre></body></html>''', encoding="utf-8")
    e = lambda text: escape(str(text), quote=True)

    def page(title, body, prefix="", filter_status=False, show_heading=True):
        status = '<span id="filter-status" role="status"></span>' if filter_status else ""
        heading = f'<div class="page-heading"><h1>{e(title)}</h1>{status}</div>' if show_heading else ""
        return f'''<!doctype html>
<html lang="ja" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · VN Mirror Archive</title><link rel="icon" href="{prefix}favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="{prefix}pico.min.css"><link rel="stylesheet" href="{prefix}style.css"><script defer src="{prefix}app.js"></script></head>
<body><a class="skip" href="#main">本文へ</a><header class="container"><nav aria-label="メイン"><ul><li><a href="{prefix}index.html"><strong>VN Mirror Archive</strong></a></li></ul><ul><li><small class="updated">更新：{e(data['generated_at'][:10])}</small></li></ul></nav><p>体験版・パッチ・デモムービーの保存庫</p></header>
<main id="main" class="container">{heading}{body}</main>
</body></html>'''

    controls = '''<form id="filters" hidden><label>作品名、ファイル名で検索<input name="q" type="search" placeholder="作品名、ファイル名"></label><div class="filter-options"><fieldset class="kind-filters"><legend>配布形態</legend><label><input type="radio" name="kind" value="" checked><span>すべて</span></label><label><input type="radio" name="kind" value="trial"><span>体験版</span></label><label><input type="radio" name="kind" value="patch"><span>パッチ</span></label><label><input type="radio" name="kind" value="full"><span>本編</span></label><label><input type="radio" name="kind" value="movie"><span>ムービー</span></label><label><input type="radio" name="kind" value="other"><span>その他</span></label></fieldset><label class="available-filter"><input name="available" type="checkbox">ダウンロード可能な配布物のみ</label></div></form>'''
    index = '<form id="brand-filters" hidden><input name="q" type="search" placeholder="ブランド名" aria-label="ブランド名で検索"></form><ul class="makers">'
    (output / "makers").mkdir(exist_ok=True)
    # Remove only pages generated by this builder, including renamed/merged makers.
    for old in (output / "makers").glob("*.html"):
        if re.fullmatch(r"[a-f0-9]{64}", old.stem):
            old.unlink()
    # Group once: rescanning every file for every product makes a full
    # archive build quadratic as additional mirror records are discovered.
    products_by_maker = {}
    files_by_product = {}
    for product in data["products"]:
        products_by_maker.setdefault(product["manufacturer_id"], []).append(product)
    for file in data["files"]:
        files_by_product.setdefault(file["product_id"], []).append(file)
    for maker in data["manufacturers"]:
        products = products_by_maker.get(maker["id"], [])
        index += f'<li data-brand="{e(maker["name"])}"><a href="makers/{maker["id"]}.html">{e(maker["name"])}</a><small>{len(products)}作品</small></li>'
        body = controls
        for product in products:
            body += f'<section class="product" id="p-{product["id"]}"><h2>{e(product["title"])}</h2>'
            for file in files_by_product.get(product["id"], []):
                query = " ".join([maker["name"], product["title"], file["name"], *[(s["filename"] or "") for s in file["sources"]]])
                label = file["name"]
                if label.startswith(product["title"]):
                    label = label[len(product["title"]):].strip() or KINDS[file["kind"]]
                label = re.sub(r"^" + re.escape(KINDS[file["kind"]]) + r"(?:\s+|$)", "", label).strip()
                extra = " " + e(label) if label else ""
                body += f'<article data-entry data-search="{e(query)}" data-kind="{file["kind"]}" data-available="{str(bool(file["download_url"])).lower()}"><h3><span class="kind">{KINDS[file["kind"]]}</span>{extra}</h3>'
                size = file["size"]
                if size is None:
                    size = next((source["expected_size"] for source in file["sources"] if source.get("expected_size") is not None), None)
                size_text = human_size(size) if size is not None else next((source["size_text"] for source in file["sources"] if source.get("size_text")), "サイズ不明")
                body += f'<div class="file-row"><p class="file-info"><span class="filename">{e(file["filename"] or "ファイル名不明")}</span><small class="file-size">{e(size_text)}</small></p>'
                if file["download_url"]:
                    body += f'<a class="download" data-id="{file["id"]}" href="{e(file["download_url"])}">ダウンロード</a>'
                else:
                    body += '<small class="unavailable">未収録</small>'
                body += '</div><details><summary>チェックサム・出典</summary>'
                info = []
                for key, name in [("version", "バージョン"), ("platform", "対象OS"), ("requirements", "適用条件")]:
                    if file[key]:
                        info.append(f'{name}: {e(file[key])}')
                if info:
                    body += '<p>' + ' · '.join(info) + '</p>'
                for key in ["sha256", "md5"]:
                    if file[key]:
                        body += f'<p>{key.upper()}: <code>{e(file[key])}</code></p>'
                body += '<ul>'
                for source in file["sources"]:
                    body += f'<li><a href="{e(source["page_url"])}">{e(source["site"])}</a> · {e(source["published_at"] or "掲載日不明")} · {e(source["filename"] or "ファイル名不明")}</li>'
                body += '</ul></details></article>'
            body += '</section>'
        (output / "makers" / (maker["id"] + ".html")).write_text(page(maker["name"], body, "../", filter_status=True), encoding="utf-8")
    index += '</ul>'
    if not data["manufacturers"]:
        index += '<p>まだ配布物が登録されていません。</p>'
    (output / "index.html").write_text(page("ブランド一覧", index, show_heading=False), encoding="utf-8")
    print(f"static site: {output} ({len(data['manufacturers'])} manufacturers, {len(data['files'])} files)")
