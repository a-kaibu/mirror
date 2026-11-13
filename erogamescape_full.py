"""Resume a full export of public ErogameScape data, excluding service internals."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import erogamescape as e
import archiver as a

SCHEMA_SQL = """SELECT json_build_object('id',1,'tables',json_agg(t))::text AS metadata FROM
(SELECT t.table_name,t.table_type,json_agg(json_build_object('name',c.column_name,'type',c.data_type,'udt',c.udt_name,'nullable',c.is_nullable,'max_length',c.character_maximum_length) ORDER BY c.ordinal_position) AS columns,
 (SELECT json_agg(a.attname ORDER BY k.ord) FROM pg_index i CROSS JOIN unnest(i.indkey) WITH ORDINALITY k(attnum,ord)
 JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
 WHERE i.indrelid=to_regclass('public.'||quote_ident(t.table_name)) AND i.indisprimary) AS primary_key
 FROM information_schema.tables t JOIN information_schema.columns c ON c.table_schema=t.table_schema AND c.table_name=t.table_name
 WHERE t.table_schema='public' GROUP BY t.table_name,t.table_type ORDER BY t.table_name) t"""
INTERNAL = {"users","personal_access_tokens","password_reset_tokens","failed_jobs","migrations",
    "myuser_preference","user_dislike_user","user_game_preferences","user_rss_articles","user_rss_feeds",
    "hoge","g","eb","tmp","temptable","temptable2","twitter_timestamp_for_api","sql_cost"}

def exclusion(table):
    name=table["table_name"]
    if name in INTERNAL or name.startswith(("mod_","delete_")) or name.endswith(("_log","_backup")):
        return "authentication, private preferences, operational logs or pending edits"
    for column in table["columns"]:
        if re.search(r"(?:^|_)(?:password|passwd|secret|token|email|session)(?:_|$)",column["name"],re.I):
            return "contains authentication/contact fields"
    return None


def quote(name):return '"'+name.replace('"','""')+'"'
def literal(value):
    if value is None:raise ValueError("NULL pagination key")
    if isinstance(value,(dict,list)):raise ValueError("compound value in pagination key")
    if isinstance(value,bool):return "TRUE" if value else "FALSE"
    if isinstance(value,(int,float)):return str(value)
    return "E'"+str(value).replace("\\","\\\\").replace("'","''")+"'"


def query_rows(sql, request=e.query):
    # Envelope IDs belong to the transport, never replace source IDs/keys.
    return [row["row"] for row in e.parse_rows(a.decode_html(request(sql)))]


def initialize(db):
    db.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS _source_schema(name TEXT PRIMARY KEY,data TEXT NOT NULL,excluded_reason TEXT);
      CREATE TABLE IF NOT EXISTS _capture_state(name TEXT PRIMARY KEY,status TEXT NOT NULL,cursor TEXT,offset INTEGER NOT NULL DEFAULT 0,rows INTEGER NOT NULL DEFAULT 0,expected_count INTEGER,source_count INTEGER,started_at TEXT,updated_at TEXT,error TEXT);
      CREATE TABLE IF NOT EXISTS _capture_requests(id INTEGER PRIMARY KEY,name TEXT,sql TEXT,response_sha256 TEXT,row_count INTEGER,fetched_at TEXT,error TEXT);
      CREATE TABLE IF NOT EXISTS _archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    """)
    db.row_factory=sqlite3.Row


def capture_batch_size(table):
    # Narrow fixed-width history tables fit comfortably inside the 16 MiB
    # response bound. The public form still enforces its cost/10-second limit.
    fixed={'smallint','integer','bigint','boolean','real','double precision','date',
           'timestamp without time zone','timestamp with time zone'}
    columns=table['columns']
    def narrow(column):
        length=column.get('max_length')
        return column['type'] in fixed or (column['type']=='character' and type(length) is int and 0<length<=16)
    return 5000 if 0<len(columns)<=16 and all(narrow(column) for column in columns) else 500


def sqlite_type(column):
    kind=column["type"]
    if kind in {"smallint","integer","bigint","boolean"}:return "INTEGER"
    if kind in {"real","double precision"}:return "REAL"
    return "TEXT"  # Preserve exact decimals, enums, dates, arrays and JSON.


def cell(value):
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False)
    return value


class QueryCostExceeded(ValueError):
    pass


def check_query_cost(raw):
    text=a.DOM(a.decode_html(raw)).root.text()
    if re.search(r"エラー\s*[(（]実行コスト高[)）]",text):
        raise QueryCostExceeded("public SQL form rejected the query cost; use smaller indexed ranges")


def transport(sql, request):
    raw=request(sql)
    # Keep numeric lexemes exact for PostgreSQL numeric/decimal columns.
    rows=[]
    root=a.DOM(a.decode_html(raw)).root
    for table in root.all({"table"}):
        nodes=list(table.all({"tr"}))
        if nodes and [n.text() for n in nodes[0].all({"td","th"})]==["metadata"]:
            for node in nodes[1:]:
                cells=list(node.all({"td"}))
                if len(cells)!=1:raise ValueError("invalid result row")
                envelope=json.loads(cells[0].text(),parse_float=str)
                if not isinstance(envelope.get("row"),dict):raise ValueError("invalid row envelope")
                rows.append(envelope["row"])
            return rows,hashlib.sha256(raw).hexdigest()
    # The official empty-results response has a successful query plan and an
    # empty query_result_main; all other missing result tables are failures.
    check_query_cost(raw)
    if e.parse_rows(a.decode_html(raw))==[]:return [],hashlib.sha256(raw).hexdigest()
    raise ValueError("SQL result missing")


def public_filter(table):
    # Honor the public profile's review visibility flag when exporting user text.
    if table["table_name"].startswith("userreview") and any(c["name"]=="uid" for c in table["columns"]):
        return "NOT EXISTS (SELECT 1 FROM myuserview visibility WHERE visibility.uid=src.uid AND visibility.review_private IS TRUE)"
    return ""


def count_source(name, request, predicate=""):

    sql="SELECT json_build_object('id',1,'count',count(*))::text AS metadata FROM "+quote(name)+" src"+(" WHERE "+predicate if predicate else "")
    raw=request(sql)
    check_query_cost(raw)
    rows=e.parse_rows(a.decode_html(raw))
    if len(rows)!=1 or type(rows[0].get("count")) is not int:raise ValueError("source count missing")
    return rows[0]["count"]


def capture(db, table, request=e.query, delay=3, max_pages=None):
    name=table["table_name"];columns=table["columns"];pk=table.get("primary_key") or []
    predicate=public_filter(table)
    state=db.execute("SELECT * FROM _capture_state WHERE name=?",(name,)).fetchone()
    if state and state["status"]=="complete":return
    definition=",".join(quote(c["name"])+" "+sqlite_type(c) for c in columns)
    if pk:definition+=",PRIMARY KEY("+",".join(map(quote,pk))+")"
    db.execute("CREATE TABLE IF NOT EXISTS "+quote(name)+"("+definition+")")
    if state:
        cursor=json.loads(state["cursor"]) if state["cursor"] else None
        offset=state["offset"];expected=state["expected_count"]
    else:
        cursor=None;offset=0;expected=None
        db.execute("INSERT INTO _capture_state(name,status,expected_count,started_at,updated_at) VALUES(?,?,?,?,?)",(name,"running",expected,a.now(),a.now()));db.commit()
    if expected is None:
        try:
            expected=count_source(name,request,predicate)
            db.execute("UPDATE _capture_state SET expected_count=? WHERE name=?",(expected,name));db.commit()
        except QueryCostExceeded:
            db.rollback()
            from erogamescape_ranges import capture_ranges
            return capture_ranges(db,table,request,delay,max_pages)
        except Exception as error:
            db.rollback()
            db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),str(error),name));db.commit()
            print("full metadata count failed",name,error,flush=True);return
    insert="INSERT INTO "+quote(name)+"("+",".join(quote(c["name"]) for c in columns)+") VALUES("+",".join("?" for _ in columns)+")"
    pages=0
    while max_pages is None or pages<max_pages:
        conditions=[predicate] if predicate else []
        if pk and cursor:
            conditions.append("("+",".join(map(quote,pk))+") > ("+",".join(map(literal,cursor))+")")
        where=" WHERE "+" AND ".join(conditions) if conditions else ""
        order=",".join(map(quote,pk)) if pk else "row_to_json(src)::text COLLATE \"C\""
        tail="" if pk else " OFFSET "+str(offset)
        sql="SELECT json_build_object('id',1,'row',row_to_json(t))::text AS metadata FROM (SELECT src.* FROM "+quote(name)+" src"+where+" ORDER BY "+order+" LIMIT "+str(capture_batch_size(table))+tail+") t"
        try:
            rows,digest=transport(sql,request)
            if rows:
                if any(set(row)!={c["name"] for c in columns} for row in rows):raise ValueError("source schema changed")
                new_cursor=[rows[-1][key] for key in pk] if pk else None
                if pk and cursor==new_cursor:raise ValueError("pagination failed to advance")
                db.executemany(insert,([cell(row[c["name"]]) for c in columns] for row in rows))
                offset+=len(rows);cursor=new_cursor
                db.execute("UPDATE _capture_state SET status='running',cursor=?,offset=?,rows=?,updated_at=?,error=NULL WHERE name=?",(json.dumps(cursor),offset,offset,a.now(),name))
            else:
                actual=db.execute("SELECT count(*) FROM "+quote(name)).fetchone()[0]
                source=count_source(name,request,predicate)
                if actual!=source:raise ValueError(f"count drift: captured {actual}, current source {source}; reconciliation required")
                db.execute("UPDATE _capture_state SET status='complete',rows=?,source_count=?,updated_at=?,error=NULL WHERE name=?",(actual,source,a.now(),name))
            db.execute("INSERT INTO _capture_requests(name,sql,response_sha256,row_count,fetched_at) VALUES(?,?,?,?,?)",(name,sql,digest,len(rows),a.now()));db.commit()
        except QueryCostExceeded as error:
            db.rollback()
            db.execute("INSERT INTO _capture_requests(name,sql,fetched_at,error) VALUES(?,?,?,?)",(name,sql,a.now(),str(error)));db.commit()
            from erogamescape_ranges import capture_ranges
            return capture_ranges(db,table,request,delay,max_pages=None if max_pages is None else max(0,max_pages-pages))
        except Exception as error:
            db.rollback()
            db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),str(error),name))
            db.execute("INSERT INTO _capture_requests(name,sql,fetched_at,error) VALUES(?,?,?,?)",(name,sql,a.now(),str(error)));db.commit()
            print("full metadata failed",name,error,flush=True);return
        pages+=1
        print("full metadata",name,offset,"complete",not rows,flush=True)
        if not rows:break
        time.sleep(delay)


def seed_catalog(db, schema, path, request):
    path=Path(path)
    if not path.exists():return
    source=sqlite3.connect(path);source.row_factory=sqlite3.Row
    try:
        if source.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise ValueError("catalog seed integrity failed")
        digest=a.digest(path)["sha256"]
        for table in schema:
            name=table["table_name"]
            if name not in {"brandlist","gamelist","holyseal"} or exclusion(table):continue
            state=db.execute("SELECT status FROM _capture_state WHERE name=?",(name,)).fetchone()
            if state and state[0]=="complete":continue
            columns=[c["name"] for c in table["columns"]]
            available={row[1] for row in source.execute("PRAGMA table_info("+quote(name)+")")}
            if available!=set(columns):continue
            # Exact decimals are not seeded from an earlier inferred-type export.
            if any(c["type"]=="numeric" for c in table["columns"]):continue
            rows=source.execute("SELECT "+",".join(map(quote,columns))+" FROM "+quote(name)).fetchall()
            count=count_source(name,request,public_filter(table))
            if len(rows)!=count:continue
            pk=table["primary_key"]
            definition=",".join(quote(c["name"])+" "+sqlite_type(c) for c in table["columns"])+",PRIMARY KEY("+",".join(map(quote,pk))+")"
            db.execute("CREATE TABLE IF NOT EXISTS "+quote(name)+"("+definition+")")
            db.execute("DELETE FROM "+quote(name))
            db.executemany("INSERT INTO "+quote(name)+"("+",".join(map(quote,columns))+") VALUES("+",".join("?" for _ in columns)+")",([row[c] for c in columns] for row in rows))
            last=db.execute("SELECT "+",".join(map(quote,pk))+" FROM "+quote(name)+" ORDER BY "+",".join(quote(k)+" DESC" for k in pk)+" LIMIT 1").fetchone()
            cursor=json.dumps(list(last)) if last else None
            db.execute("INSERT OR REPLACE INTO _capture_state VALUES(?,?,?,?,?,?,?,?,?,?)",(name,"complete",cursor,count,count,count,count,a.now(),a.now(),None))
            db.execute("INSERT INTO _capture_requests(name,sql,response_sha256,row_count,fetched_at) VALUES(?,?,?,?,?)",(name,"seed validated catalog snapshot",digest,count,a.now()));db.commit()
            print("full metadata seeded",name,count,flush=True)
    finally:source.close()


def collect(path, request=e.query, max_tables=None, max_pages=None, delay=3):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30);initialize(db)
    try:
        schema=e.parse_rows(a.decode_html(request(SCHEMA_SQL)))[0]["tables"]
        if not schema:raise ValueError("public schema missing")
        for table in schema:
            db.execute("INSERT OR REPLACE INTO _source_schema VALUES(?,?,?)",(table["table_name"],json.dumps(table,ensure_ascii=False),exclusion(table)))
        db.executemany("INSERT OR REPLACE INTO _archive_info VALUES(?,?)",[("source",e.FORM),("schema_fetched_at",a.now()),("scope","public data; excluded service internals are listed in _source_schema")]);db.commit()
        seed_catalog(db,schema,Path(path).with_name("erogamescape.sqlite3"),request)
        targets=[t for t in schema if not exclusion(t)]
        priority={"brandlist":0,"gamelist":1,"holyseal":2,"createrlist":3,"shokushu":4}
        targets.sort(key=lambda t:(priority.get(t["table_name"],5),t["table_name"]))
        attempted=0
        for table in targets:
            state=db.execute("SELECT status FROM _capture_state WHERE name=?",(table["table_name"],)).fetchone()
            if state and state[0]=="complete":continue
            if max_tables is not None and attempted>=max_tables:break
            from archive_lock import exclusive_file
            claim=Path(path).parent/'.claims'/('erogamescape-table-'+hashlib.sha256(table["table_name"].encode()).hexdigest()+'.lock')
            try:
                with exclusive_file(claim,blocking=False):
                    planned=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='_range_plans'").fetchone()
                    if planned:planned=db.execute("SELECT 1 FROM _range_plans WHERE name=?",(table["table_name"],)).fetchone()
                    if planned:
                        from erogamescape_ranges import capture_ranges
                        capture_ranges(db,table,request,delay,max_pages)
                    else:capture(db,table,request,delay,max_pages)
            except BlockingIOError:
                print("full metadata deferred busy table",table["table_name"],flush=True)
                continue
            attempted+=1
        print("full metadata states",[tuple(r) for r in db.execute("SELECT status,count(*) FROM _capture_state GROUP BY status")],flush=True)
        return len(targets),db.execute("SELECT count(*) FROM _capture_state WHERE status='complete'").fetchone()[0]
    finally:db.close()


def publish(path):
    db=sqlite3.connect(path,timeout=30);initialize(db)
    try:
        pending=db.execute("""SELECT s.name,coalesce(c.status,'missing') FROM _source_schema s
            LEFT JOIN _capture_state c ON c.name=s.name WHERE s.excluded_reason IS NULL
            AND (c.status IS NULL OR c.status!='complete')""").fetchall()
        if pending:raise ValueError("full export incomplete: "+str([tuple(r) for r in pending][:10]))
        if not db.execute("SELECT count(*) FROM _source_schema").fetchone()[0]:raise ValueError("empty source schema")
        for row in db.execute("SELECT name,data FROM _source_schema WHERE excluded_reason IS NULL").fetchall():
            name=row[0];schema=json.loads(row[1])
            columns={r[1] for r in db.execute("PRAGMA table_info("+quote(name)+")")}
            if columns!={c["name"] for c in schema["columns"]}:raise ValueError("snapshot schema mismatch: "+name)
            count=db.execute("SELECT count(*) FROM "+quote(name)).fetchone()[0]
            state=db.execute("SELECT rows,source_count FROM _capture_state WHERE name=?",(name,)).fetchone()
            if count!=state[0] or count!=state[1]:raise ValueError("snapshot count mismatch: "+name)
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise ValueError("full database integrity failed")
        db.execute("INSERT OR REPLACE INTO _archive_info VALUES('exported_at',?)",(a.now(),));db.commit()
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:raise ValueError("SQLite checkpoint busy")
        # Keep the self-contained database file stable during upload/read-back;
        # no second file-sized backup is required for a large metadata database.
        db.execute("BEGIN IMMEDIATE")
        e.upload_snapshot(Path(path))
    finally:
        db.rollback();db.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path",default="archive/erogamescape.full.sqlite3")
    parser.add_argument("--max-tables",type=a.positive);parser.add_argument("--max-pages",type=a.positive)
    parser.add_argument("--upload",action="store_true")
    args=parser.parse_args()
    print("full metadata coverage",collect(args.path,max_tables=args.max_tables,max_pages=args.max_pages),flush=True)
    if args.upload:publish(args.path)
