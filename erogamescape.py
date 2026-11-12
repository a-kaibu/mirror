"""Import public ErogameScape catalog metadata through its read-only SQL form."""
import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
import time
from urllib.parse import urlencode, parse_qs, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import archiver as a
from catalog import normalized

FORM = "https://erogamescape.dyndns.org/~ap2/ero/toukei_kaiseki/sql_for_erogamer_form.php"
TABLES = {
    "brand": ("brandlist", "*"),
    "game": ("gamelist", "*"),
    "holyseal": ("holyseal", "*"),
}


def initialize(store):
    store.db.executescript("""
      CREATE TABLE IF NOT EXISTS external_metadata(provider TEXT,entity TEXT,external_id INTEGER,data TEXT NOT NULL,fetched_at TEXT NOT NULL,PRIMARY KEY(provider,entity,external_id));
      CREATE TABLE IF NOT EXISTS metadata_sync(entity TEXT PRIMARY KEY,cursor INTEGER NOT NULL,complete INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS metadata_fetches(query_hash TEXT PRIMARY KEY,sql TEXT NOT NULL,path TEXT,fetched_at TEXT NOT NULL,row_count INTEGER,error TEXT);
      CREATE TABLE IF NOT EXISTS metadata_links(entity TEXT,local_id TEXT,external_id INTEGER,method TEXT NOT NULL,PRIMARY KEY(entity,local_id,external_id));
    """)


def parse_rows(html):
    root = a.DOM(html).root
    for table in root.all({"table"}):
        rows = list(table.all({"tr"}))
        if not rows:
            continue
        header = [n.text() for n in rows[0].all({"th", "td"})]
        if header != ["metadata"]:
            continue
        records = []
        for row in rows[1:]:
            cells = list(row.all({"td"}))
            if len(cells) != 1:
                raise ValueError("unexpected SQL result row")
            record = json.loads(cells[0].text())
            if not isinstance(record, dict) or type(record.get("id")) is not int:
                raise ValueError("metadata row has no integer ID")
            records.append(record)
        return records
    elements = {n.attrs.get("id"): n for n in root.all({"div"}) if n.attrs.get("id")}
    result = elements.get("query_result_main")
    plan = elements.get("queryplan_main")
    if (result is not None and not result.children and plan is not None and plan.text()
            and not re.search(r"SQLSTATE|ERROR|エラー|Fatal|Warning", root.text(), re.I)):
        return []
    raise ValueError("SQL result table missing; refusing an empty successful import")


_sql_last = 0
_sql_not_before = 0


def query(sql):
    global _sql_last, _sql_not_before
    request = Request(FORM, data=urlencode({"sql": sql}).encode(), headers={
        "User-Agent": "VN-Mirror-Archiver/0.1 (cultural preservation)",
        "Content-Type": "application/x-www-form-urlencoded"})
    for attempt in range(3):
        current=time.monotonic()
        time.sleep(max(0,3-(current-_sql_last),_sql_not_before-current))
        _sql_last=time.monotonic()
        try:
            with urlopen(request, timeout=40) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data)>16*1024*1024:raise ValueError("SQL response exceeds 16 MiB")
            return data
        except HTTPError as error:
            if error.code not in {429,500,502,503,504}:raise
            wait=a.retry_after_delay(error.headers.get("Retry-After",""),60 if error.code==429 else 2**attempt)
            _sql_not_before=time.monotonic()+wait
            error.close()
            if attempt==2:raise
        except OSError:
            _sql_not_before=time.monotonic()+2**attempt
            if attempt==2:raise


def collect(store, entity, request=query, max_pages=None, refresh=False, delay=3):
    initialize(store)
    saved = store.db.execute("SELECT cursor FROM metadata_sync WHERE entity=?", (entity,)).fetchone()
    cursor = 0 if refresh or not saved else saved[0]
    table, columns = TABLES[entity]
    pages = total = 0
    while max_pages is None or pages < max_pages:
        sql = f"SELECT row_to_json(t)::text AS metadata FROM (SELECT {columns} FROM {table} WHERE id>{cursor} ORDER BY id LIMIT 500) t"
        key = hashlib.sha256(sql.encode()).hexdigest()
        rel = "html/erogamescape-" + key + ".html"
        try:
            raw = request(sql)
            (store.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (store.root / rel).write_bytes(raw)
            rows = parse_rows(a.decode_html(raw))
            ids = [row["id"] for row in rows]
            if ids and (ids != sorted(set(ids)) or ids[0] <= cursor):
                raise ValueError("SQL keyset pagination did not advance")
            for row in rows:
                store.db.execute("INSERT OR REPLACE INTO external_metadata VALUES(?,?,?,?,?)", ("erogamescape",entity,row["id"],json.dumps(row,ensure_ascii=False),a.now()))
            if ids:
                cursor = ids[-1]
            store.db.execute("INSERT OR REPLACE INTO metadata_sync VALUES(?,?,?,?)", (entity,cursor,int(not rows),a.now()))
            store.db.execute("INSERT OR REPLACE INTO metadata_fetches VALUES(?,?,?,?,?,?)", (key,sql,rel,a.now(),len(rows),None))
            store.db.commit()
        except Exception as error:
            store.db.rollback()
            store.db.execute("INSERT OR REPLACE INTO metadata_fetches VALUES(?,?,?,?,?,?)", (key,sql,rel if (store.root/rel).exists() else None,a.now(),None,str(error)))
            store.db.commit()
            raise
        total += len(rows);pages += 1
        print("erogamescape",entity,"rows",total,"cursor",cursor,"complete",not rows,flush=True)
        if not rows:
            break
        if delay:
            time.sleep(delay)
    return total


def match(store):
    initialize(store)
    data = {entity: {} for entity in TABLES}
    for row in store.db.execute("SELECT entity,external_id,data FROM external_metadata WHERE provider='erogamescape'"):
        data[row[0]][row[1]] = json.loads(row[2])
    by_name = {}
    for ident, brand in data["brand"].items():
        by_name.setdefault(normalized(brand["brandname"]),set()).add(ident)
    store.db.execute("DELETE FROM metadata_links WHERE method IN ('normalized_name','brand_and_title','holyseal_id')")
    brand_links = {}
    for maker in store.db.execute("SELECT * FROM manufacturers").fetchall():
        names = [r[0] for r in store.db.execute("SELECT alias FROM manufacturer_aliases WHERE manufacturer_id=?",(maker["id"],))]
        candidates = set().union(*(by_name.get(name,set()) for name in names))
        if len(candidates) == 1:
            external_id = next(iter(candidates));brand_links[maker["id"]]=external_id
            store.db.execute("INSERT OR IGNORE INTO metadata_links VALUES(?,?,?,?)",("brand",maker["id"],external_id,"normalized_name"))
    holy_games = {}
    for row in data["holyseal"].values():
        holy_games.setdefault(row["holyseal_id"],set()).add(row["game"])
    games_by_title = {}
    for ident, game in data["game"].items():
        games_by_title.setdefault((game["brandname"],normalized(game["gamename"])),set()).add(ident)
    for product in store.db.execute("SELECT * FROM products").fetchall():
        candidates = set()
        for row in store.db.execute("SELECT i.data FROM items i JOIN item_catalog c ON c.item_id=i.id WHERE c.product_id=? AND i.site='holyseal'",(product["id"],)):
            url = json.loads(row[0]).get("original_page_url","")
            code = parse_qs(urlsplit(url).query).get("prdcode",[""])[0]
            if code.isdigit():candidates.update(holy_games.get(int(code),set()))
        method = "holyseal_id"
        if not candidates:
            candidates = games_by_title.get((brand_links.get(product["manufacturer_id"]),normalized(product["title"])),set())
            method = "brand_and_title"
        if len(candidates)==1 and next(iter(candidates)) in data["game"]:
            store.db.execute("INSERT OR IGNORE INTO metadata_links VALUES(?,?,?,?)",("game",product["id"],next(iter(candidates)),method))
    store.db.commit()
    return [tuple(row) for row in store.db.execute("SELECT entity,count(*) FROM metadata_links GROUP BY entity")]


def verify_counts(store, request=query):
    expressions = ",".join("'"+entity+"',(SELECT count(*) FROM "+table+")" for entity,(table,_) in TABLES.items())
    sql = "SELECT json_build_object('id',1," + expressions + ")::text AS metadata"
    result = parse_rows(a.decode_html(request(sql)))
    if len(result)!=1:raise ValueError("metadata count response missing")
    counts = result[0]
    for entity in TABLES:
        actual = store.db.execute("SELECT count(*) FROM external_metadata WHERE provider='erogamescape' AND entity=?",(entity,)).fetchone()[0]
        if type(counts.get(entity)) is not int or counts[entity] != actual:
            raise ValueError(f"metadata count mismatch for {entity}: remote {counts.get(entity)}, local {actual}")
    return counts


def export_sqlite(store, path=None):
    """Build a standalone catalog snapshot only after all three scans finish."""
    target = Path(path) if path else store.root / "erogamescape.sqlite3"
    store.db.execute("BEGIN")
    try:
        sync = [dict(r) for r in store.db.execute("SELECT * FROM metadata_sync")]
        if {r["entity"] for r in sync if r["complete"]} != set(TABLES):
            raise ValueError("metadata import incomplete; refusing a full snapshot")
        rows = [dict(r) for r in store.db.execute("SELECT * FROM external_metadata WHERE provider='erogamescape' ORDER BY entity,external_id")]
    finally:
        store.db.rollback()
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix="erogamescape-", suffix=".sqlite3",delete=False) as stream:
        temp = Path(stream.name)
    db = sqlite3.connect(temp)
    quote = lambda value: '"' + value.replace('"','""') + '"'
    try:
        db.execute("CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("CREATE TABLE provenance(entity TEXT,external_id INTEGER,fetched_at TEXT NOT NULL,PRIMARY KEY(entity,external_id))")
        for entity, (table, _) in TABLES.items():
            records = [(json.loads(row["data"]),row["fetched_at"]) for row in rows if row["entity"]==entity]
            if not records:
                raise ValueError("empty metadata table: " + entity)
            columns = ["id", *sorted(set().union(*(record.keys() for record,_ in records)) - {"id"})]
            def sql_type(column):
                values = [record.get(column) for record,_ in records if record.get(column) is not None]
                if values and all(isinstance(v,(int,bool)) for v in values):return "INTEGER"
                if values and all(isinstance(v,(int,float)) for v in values):return "REAL"
                return "TEXT"
            definition = ",".join(quote(c) + (" INTEGER PRIMARY KEY" if c=="id" else " " + sql_type(c)) for c in columns)
            db.execute("CREATE TABLE " + quote(table) + "(" + definition + ")")
            insert = "INSERT INTO " + quote(table) + "(" + ",".join(map(quote,columns)) + ") VALUES(" + ",".join("?" for _ in columns) + ")"
            def cell(value):
                return json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else value
            db.executemany(insert, ([cell(record.get(c)) for c in columns] for record,_ in records))
            db.executemany("INSERT INTO provenance VALUES(?,?,?)", ((entity,record["id"],fetched_at) for record,fetched_at in records))
            db.execute("INSERT INTO archive_info VALUES(?,?)", (entity+"_count",str(len(records))))
        db.execute("CREATE INDEX gamelist_brand ON gamelist(brandname)")
        db.execute("CREATE INDEX holyseal_game ON holyseal(game)")
        db.executemany("INSERT INTO archive_info VALUES(?,?)", [("source",FORM),("exported_at",a.now()),("sync",json.dumps(sync))])
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":raise ValueError("metadata SQLite integrity failed")
        db.close()
        temp.replace(target)
    except BaseException:
        db.close();temp.unlink(missing_ok=True);raise
    return target


def upload_snapshot(path):
    expected = a.digest(path)
    client = a.s3_client("vn-archive","hf")
    client.upload_file(str(path),"mirrors","erogamescape.sqlite3",ExtraArgs={"ContentType":"application/vnd.sqlite3","Metadata":{"sha256":expected["sha256"]}})
    if a.public_digest("vn-archive","mirrors","erogamescape.sqlite3") != (expected["sha256"],expected["size"]):
        raise ValueError("metadata public read-back mismatch")
    print("metadata SQLite publicly verified",expected["sha256"],expected["size"],flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default="archive")
    parser.add_argument("--entity",choices=["all",*TABLES],default="all")
    parser.add_argument("--max-pages",type=a.positive)
    parser.add_argument("--refresh",action="store_true")
    parser.add_argument("--export",action="store_true")
    parser.add_argument("--upload",action="store_true")
    args=parser.parse_args()
    store=a.Store(args.root)
    try:
        for entity in TABLES if args.entity=="all" else [args.entity]:
            collect(store,entity,max_pages=args.max_pages,refresh=args.refresh)
        print("metadata links",match(store),flush=True)
        if args.export or args.upload:
            print("remote/local counts verified",verify_counts(store),flush=True)
            path=export_sqlite(store)
            print("metadata SQLite",path,flush=True)
            if args.upload:upload_snapshot(path)
    finally:
        store.db.close()
