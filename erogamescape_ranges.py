"""Resume indexed key ranges when the public SQL form rejects a broad scan."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
import archiver as a
import erogamescape_full as f


def split_text(lower, upper):
    if upper is None:
        return lower + chr(128)
    if not lower < upper:
        raise ValueError("invalid text key range")
    common = 0
    while common < min(len(lower),len(upper)) and lower[common] == upper[common]:
        common += 1
    if common == len(lower):
        point = ord(upper[common])
        if point > 1:
            pivot = lower + chr(point//2)
        elif common+1 < len(upper):
            pivot = lower + chr(1)
        else:
            raise ValueError("leading key cannot be split further; secondary-key partition required")
    else:
        left,right = ord(lower[common]),ord(upper[common])
        pivot = lower[:common]+chr((left+right)//2) if right-left>1 else lower+chr(128)
    # PostgreSQL text excludes NUL; also avoid surrogate code points.
    if any(0xD800 <= ord(c) <= 0xDFFF for c in pivot):
        pivot = lower+chr(128)
    if not lower < pivot < upper:
        raise ValueError("no valid text range split")
    return pivot


def key_domain(table, request):
    key = table["primary_key"][0]
    kind = next(c["type"] for c in table["columns"] if c["name"]==key)
    bits = {"smallint":16,"integer":32,"bigint":64}
    if kind in bits:
        return key,"integer",-(2**(bits[kind]-1)),2**(bits[kind]-1)
    if kind not in {"text","character varying","character"}:
        raise ValueError("indexed range export does not support leading key type: "+kind)
    sql = ("SELECT json_build_object('id',1,'collation',c.collname,'database_collation',to_jsonb(d)->>'datcollate')::text AS metadata "
           "FROM pg_attribute att JOIN pg_collation c ON att.attcollation=c.oid CROSS JOIN pg_database d "
           "WHERE att.attrelid=to_regclass("+f.literal("public."+f.quote(table["table_name"]))+") "
           "AND att.attname="+f.literal(key)+" AND d.datname=current_database()")
    rows=f.e.parse_rows(a.decode_html(request(sql)))
    if len(rows)!=1:
        raise ValueError("text key collation could not be verified")
    collation=rows[0].get("collation")
    if collation=="default":collation=rows[0].get("database_collation")
    if collation not in {"C","POSIX","C.UTF-8","C.utf8"}:
        raise ValueError("range partition requires a verified binary text collation")
    return key,"text","",None


def bounded_count(db,table,request,key,kind,lower,upper):
    """Count the complete key domain if even the broad count is cost-limited."""
    queue=[(lower,upper,0)];proof=[];total=0
    while queue:
        lo,hi,depth=queue.pop()
        parts=[f.quote(key)+" >= "+f.literal(lo)]
        if hi is not None:parts.append(f.quote(key)+" < "+f.literal(hi))
        public=f.public_filter(table)
        if public:parts.append(public)
        try:
            value=f.count_source(table["table_name"],request," AND ".join(parts))
            total+=value;proof.append({"lower":lo,"upper":hi,"count":value,"checked_at":a.now()})
        except f.QueryCostExceeded:
            if depth>=128:raise ValueError("range count split depth exceeded")
            pivot=split_text(lo,hi) if kind=="text" else (lo+hi)//2
            if pivot==lo or pivot==hi:raise ValueError("count range cannot be split further")
            queue.extend([(pivot,hi,depth+1),(lo,pivot,depth+1)])
    db.execute("INSERT OR REPLACE INTO _archive_info VALUES(?,?)",("range_count:"+table["table_name"],json.dumps(proof,ensure_ascii=False)))
    return total


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS _range_plans(name TEXT PRIMARY KEY,key_name TEXT,kind TEXT,base_cursor TEXT,created_at TEXT);
      CREATE TABLE IF NOT EXISTS _capture_ranges(name TEXT,path TEXT,lower_key TEXT,upper_key TEXT,status TEXT,
        updated_at TEXT,error TEXT,PRIMARY KEY(name,path));
    """)


def _capture_ranges(db, table, request=f.e.query, delay=3, max_pages=None):
    name=table["table_name"]
    if f.exclusion(table):raise ValueError("excluded table cannot be exported")
    pk=table.get("primary_key") or []
    if not pk:raise ValueError("cost-limited range export requires a primary key")
    initialize(db)
    state=db.execute("SELECT * FROM _capture_state WHERE name=?",(name,)).fetchone()
    if state and state["status"]=="complete":return
    if not state:raise ValueError("initialize the regular table capture before range export")
    cursor=json.loads(state["cursor"]) if state["cursor"] else None
    offset=state["rows"]
    if db.execute("SELECT count(*) FROM "+f.quote(name)).fetchone()[0]!=offset:
        raise ValueError("local row count differs from capture state")
    key,kind,domain_lower,domain_upper=key_domain(table,request)
    plan=db.execute("SELECT * FROM _range_plans WHERE name=?",(name,)).fetchone()
    if plan:
        if plan["key_name"]!=key or plan["kind"]!=kind:raise ValueError("range key schema changed")
    else:
        lower,upper=domain_lower,domain_upper
        if cursor:lower=cursor[0]
        db.execute("INSERT INTO _range_plans VALUES(?,?,?,?,?)",(name,key,kind,json.dumps(cursor),a.now()))
        db.execute("INSERT INTO _capture_ranges VALUES(?,?,?,?,?,?,?)",(name,"",json.dumps(lower),json.dumps(upper),"pending",a.now(),None))
    db.execute("UPDATE _capture_state SET status='running',error=NULL,source_count=NULL,updated_at=? WHERE name=?",(a.now(),name));db.commit()
    columns=table["columns"]
    insert="INSERT INTO "+f.quote(name)+"("+",".join(f.quote(c["name"]) for c in columns)+") VALUES("+",".join("?" for _ in columns)+")"
    pages=0
    while max_pages is None or pages<max_pages:
        segment=db.execute("SELECT * FROM _capture_ranges WHERE name=? AND status='pending' ORDER BY path LIMIT 1",(name,)).fetchone()
        if segment is None:
            try:
                actual=db.execute("SELECT count(*) FROM "+f.quote(name)).fetchone()[0]
                try:source=f.count_source(name,request,f.public_filter(table))
                except f.QueryCostExceeded:
                    source=bounded_count(db,table,request,key,kind,domain_lower,domain_upper)
                if actual!=source:raise ValueError(f"count drift: captured {actual}, current source {source}; reconciliation required")
                db.execute("UPDATE _capture_state SET status='complete',rows=?,source_count=?,updated_at=?,error=NULL WHERE name=?",(actual,source,a.now(),name));db.commit()
                print("full range metadata complete",name,actual,flush=True)
            except Exception as error:
                db.rollback();db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),str(error),name));db.commit()
                print("full range metadata verification failed",name,error,flush=True)
            return
        lower,upper=json.loads(segment["lower_key"]),json.loads(segment["upper_key"])
        conditions=[f.quote(key)+" >= "+f.literal(lower)]
        if upper is not None:conditions.append(f.quote(key)+" < "+f.literal(upper))
        public=f.public_filter(table)
        if public:conditions.append(public)
        if cursor:conditions.append("("+",".join(map(f.quote,pk))+") > ("+",".join(map(f.literal,cursor))+")")
        sql=("SELECT json_build_object('id',1,'row',row_to_json(t))::text AS metadata FROM (SELECT src.* FROM "+f.quote(name)+" src WHERE "+" AND ".join(conditions)+" ORDER BY "+",".join(map(f.quote,pk))+" LIMIT "+str(f.capture_batch_size(table))+") t")
        try:
            rows,digest=f.transport(sql,request)
            if rows:
                if any(set(row)!={c["name"] for c in columns} for row in rows):raise ValueError("source schema changed")
                new_cursor=[rows[-1][k] for k in pk]
                if cursor==new_cursor:raise ValueError("pagination failed to advance")
                db.executemany(insert,([f.cell(row[c["name"]]) for c in columns] for row in rows))
                cursor=new_cursor;offset+=len(rows)
                db.execute("UPDATE _capture_state SET cursor=?,offset=?,rows=?,updated_at=? WHERE name=?",(json.dumps(cursor),offset,offset,a.now(),name))
            else:
                db.execute("UPDATE _capture_ranges SET status='complete',updated_at=?,error=NULL WHERE name=? AND path=?",(a.now(),name,segment["path"]))
            db.execute("INSERT INTO _capture_requests(name,sql,response_sha256,row_count,fetched_at) VALUES(?,?,?,?,?)",(name,sql,digest,len(rows),a.now()));db.commit()
            print("full range metadata",name,offset,"range",repr(lower),repr(upper),"exhausted",not rows,flush=True)
        except f.QueryCostExceeded as error:
            db.rollback()
            try:
                if len(segment["path"])>=128:raise ValueError("range split depth exceeded")
                pivot=split_text(lower,upper) if kind=="text" else (lower+upper)//2
                if pivot==lower or pivot==upper:raise ValueError("leading key cannot be split further")
                db.execute("UPDATE _capture_ranges SET status='split',updated_at=?,error=? WHERE name=? AND path=?",(a.now(),str(error),name,segment["path"]))
                for suffix,lo,hi in [("0",lower,pivot),("1",pivot,upper)]:
                    db.execute("INSERT INTO _capture_ranges VALUES(?,?,?,?,?,?,?)",(name,segment["path"]+suffix,json.dumps(lo),json.dumps(hi),"pending",a.now(),None))
                db.execute("INSERT INTO _capture_requests(name,sql,fetched_at,error) VALUES(?,?,?,?)",(name,sql,a.now(),str(error)));db.commit()
                print("full range split",name,repr(lower),repr(upper),"at",repr(pivot),flush=True)
            except Exception as split_error:
                db.rollback();db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),str(split_error),name));db.commit();raise
        except Exception as error:
            db.rollback()
            db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),str(error),name))
            db.execute("INSERT INTO _capture_requests(name,sql,fetched_at,error) VALUES(?,?,?,?)",(name,sql,a.now(),str(error)));db.commit()
            print("full range metadata failed",name,error,flush=True);return
        pages+=1
        time.sleep(delay)


def capture_ranges(db, table, request=f.e.query, delay=3, max_pages=None):
    if f.exclusion(table):raise ValueError("excluded table cannot be exported")
    try:
        return _capture_ranges(db,table,request,delay,max_pages)
    except Exception as error:
        db.rollback()
        db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),str(error),table["table_name"]))
        db.commit()
        print("full range metadata failed",table["table_name"],error,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path",default="archive/erogamescape.full.sqlite3")
    parser.add_argument("--table",required=True)
    parser.add_argument("--max-pages",type=a.positive)
    args=parser.parse_args()
    db=sqlite3.connect(args.path,timeout=30);f.initialize(db)
    try:
        row=db.execute("SELECT data,excluded_reason FROM _source_schema WHERE name=?",(args.table,)).fetchone()
        if not row or row["excluded_reason"]:raise ValueError("table is not in the public export scope")
        capture_ranges(db,json.loads(row["data"]),max_pages=args.max_pages)
    finally:db.close()
