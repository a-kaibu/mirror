"""Count-verified timestamp partitions for public history tables without a key.

Identical rows remain separate observations. Each partition is staged before
its rows and receipt become visible together; resuming never appends a partial
partition twice. No PostgreSQL physical row identifiers are used.
"""
from datetime import datetime, timedelta
import json
import time
import erogamescape_full as f
import archiver as a

LOWER = '1900-01-01T00:00:00'
UPPER = '2100-01-01T00:00:00'
BATCH = 5000


def timestamp_key(table):
    if table.get('primary_key'):
        raise ValueError('timestamp partitioning requires a table without a primary key')
    candidates = [c['name'] for c in table['columns']
                  if c['type'] == 'timestamp without time zone' and c.get('nullable') == 'NO']
    if len(candidates) != 1:
        raise ValueError('timestamp partitioning requires one non-null timestamp without time zone')
    return candidates[0]


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS _timestamp_plans(
        name TEXT PRIMARY KEY,key TEXT NOT NULL,lower TEXT NOT NULL,upper TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS _timestamp_ranges(
        name TEXT,lower TEXT,upper TEXT,status TEXT NOT NULL,fetched INTEGER NOT NULL DEFAULT 0,
        source_count INTEGER,PRIMARY KEY(name,lower,upper));
      CREATE TABLE IF NOT EXISTS _timestamp_pending(
        name TEXT,lower TEXT,upper TEXT,ordinal INTEGER,payload TEXT NOT NULL,
        PRIMARY KEY(name,lower,upper,ordinal));
    """)
    columns={r[1] for r in db.execute('PRAGMA table_info(_timestamp_ranges)')}
    if 'page_size' not in columns:
        db.execute('ALTER TABLE _timestamp_ranges ADD COLUMN page_size INTEGER NOT NULL DEFAULT 5000')
        db.commit()


def predicate(key, lower, upper):
    return f.quote(key)+' >= '+f.literal(lower)+' AND '+f.quote(key)+' < '+f.literal(upper)


def midpoint(lower, upper):
    left = datetime.fromisoformat(lower);right = datetime.fromisoformat(upper)
    span = right-left
    micros = (span.days*86400+span.seconds)*1000000+span.microseconds
    if micros <= 1:
        return None
    return (left+timedelta(microseconds=micros//2)).isoformat(timespec='microseconds')


def record_request(db, name, sql, digest, count):
    db.execute('INSERT INTO _capture_requests(name,sql,response_sha256,row_count,fetched_at) VALUES(?,?,?,?,?)',
               (name, sql, digest, count, a.now()))


def outside_count(table, key, request):
    # These two indexed counts prove that the chosen finite domain covers the
    # actual source. Never silently discard BC, infinity or far-future dates.
    return sum(f.count_source(table['table_name'], request, condition) for condition in
               (f.quote(key)+' < '+f.literal(LOWER), f.quote(key)+' >= '+f.literal(UPPER)))


def capture(db, table, request=f.e.query, delay=3, max_pages=None):
    name = table['table_name'];key = timestamp_key(table);initialize(db)
    state = db.execute('SELECT * FROM _capture_state WHERE name=?',(name,)).fetchone()
    if state and state['status'] == 'complete':
        return
    try:
        if f.public_filter(table):
            raise ValueError('visibility-filtered tables require a filter-aware partition plan')
        if outside_count(table,key,request):
            raise ValueError('source timestamps outside verified partition domain; expand the domain before export')
        plan = db.execute('SELECT * FROM _timestamp_plans WHERE name=?',(name,)).fetchone()
        if plan:
            if (plan['key'],plan['lower'],plan['upper']) != (key,LOWER,UPPER):
                raise ValueError('timestamp partition plan changed')
        else:
            if db.execute('SELECT count(*) FROM '+f.quote(name)).fetchone()[0]:
                raise ValueError('unpartitioned history rows require reconciliation before timestamp export')
            db.execute('INSERT INTO _timestamp_plans VALUES(?,?,?,?)',(name,key,LOWER,UPPER))
            db.execute('INSERT INTO _timestamp_ranges(name,lower,upper,status) VALUES(?,?,?,?)',
                       (name,LOWER,UPPER,'pending'));db.commit()
        db.execute("UPDATE _capture_state SET status='running',error=NULL,updated_at=? WHERE name=?",(a.now(),name));db.commit()
        pages = 0
        while max_pages is None or pages < max_pages:
            leaf = db.execute("SELECT * FROM _timestamp_ranges WHERE name=? AND status='pending' ORDER BY lower LIMIT 1",(name,)).fetchone()
            if leaf is None:
                break
            lower,upper = leaf['lower'],leaf['upper'];where = predicate(key,lower,upper)
            source = leaf['source_count'];middle = midpoint(lower,upper)
            try:
                if source is None:
                    source = f.count_source(name,request,where)
            except f.QueryCostExceeded:
                if middle is None:
                    raise
                source = None
            if (source is None or source > BATCH) and middle is not None and not leaf['fetched']:
                db.execute("UPDATE _timestamp_ranges SET status='split' WHERE name=? AND lower=? AND upper=?",(name,lower,upper))
                db.executemany('INSERT INTO _timestamp_ranges(name,lower,upper,status) VALUES(?,?,?,?)',
                               [(name,lower,middle,'pending'),(name,middle,upper,'pending')]);db.commit()
                if delay:time.sleep(delay)
                continue
            fetched = leaf['fetched']
            page_size = min(BATCH,leaf['page_size'])
            if page_size < 1:raise ValueError('invalid timestamp page size')
            sql = ("SELECT json_build_object('id',1,'row',row_to_json(t))::text AS metadata FROM (SELECT src.* FROM "
                   +f.quote(name)+' src WHERE '+where+' ORDER BY row_to_json(src)::text COLLATE "C" LIMIT '
                   +str(page_size)+' OFFSET '+str(fetched)+') t')
            try:
                rows,digest = f.transport(sql,request)
            except ValueError as error:
                if str(error) != 'SQL response exceeds 16 MiB' or page_size <= 1:raise
                # The rejected response is never staged. Keep the exact offset
                # and lower only the transport size, including for equal timestamps.
                db.execute('UPDATE _timestamp_ranges SET page_size=? WHERE name=? AND lower=? AND upper=?',
                           (max(1,page_size//2),name,lower,upper));db.commit()
                if delay:time.sleep(delay)
                continue
            expected_columns = {c['name'] for c in table['columns']}
            for row in rows:
                if set(row) != expected_columns:
                    raise ValueError('source schema changed during timestamp export')
                value = datetime.fromisoformat(row[key])
                if not datetime.fromisoformat(lower) <= value < datetime.fromisoformat(upper):
                    raise ValueError('source row outside timestamp partition')
            if fetched+len(rows) > source:
                raise ValueError('timestamp partition grew; reconciliation required')
            db.executemany('INSERT INTO _timestamp_pending VALUES(?,?,?,?,?)',
                [(name,lower,upper,fetched+i,json.dumps(row,ensure_ascii=False)) for i,row in enumerate(rows)])
            fetched += len(rows)
            db.execute('UPDATE _timestamp_ranges SET fetched=?,source_count=? WHERE name=? AND lower=? AND upper=?',
                       (fetched,source,name,lower,upper))
            record_request(db,name,sql,digest,len(rows));db.commit();pages += 1
            if len(rows) < page_size:
                current = f.count_source(name,request,where)
                if fetched != source or current != source:
                    raise ValueError('timestamp partition count drift; reconciliation required')
                staged = db.execute('SELECT count(*) FROM _timestamp_pending WHERE name=? AND lower=? AND upper=?',(name,lower,upper)).fetchone()[0]
                if staged != source:
                    raise ValueError('timestamp staging count mismatch')
                insert = 'INSERT INTO '+f.quote(name)+'('+','.join(f.quote(c['name']) for c in table['columns'])+') VALUES('+','.join('?' for c in table['columns'])+')'
                cursor = db.execute('SELECT payload FROM _timestamp_pending WHERE name=? AND lower=? AND upper=? ORDER BY ordinal',(name,lower,upper))
                while batch := cursor.fetchmany(BATCH):
                    records = [json.loads(r[0]) for r in batch]
                    db.executemany(insert,([f.cell(row[c['name']]) for c in table['columns']] for row in records))
                db.execute("UPDATE _timestamp_ranges SET status='complete' WHERE name=? AND lower=? AND upper=?",(name,lower,upper))
                db.execute('DELETE FROM _timestamp_pending WHERE name=? AND lower=? AND upper=?',(name,lower,upper))
                total = db.execute("SELECT coalesce(sum(source_count),0) FROM _timestamp_ranges WHERE name=? AND status='complete'",(name,)).fetchone()[0]
                db.execute('UPDATE _capture_state SET rows=?,offset=?,updated_at=? WHERE name=?',(total,total,a.now(),name));db.commit()
                print('full timestamp metadata',name,total,lower,upper,flush=True)
            if delay:time.sleep(delay)
        if db.execute("SELECT 1 FROM _timestamp_ranges WHERE name=? AND status='pending'",(name,)).fetchone():
            return
        # Recheck every leaf, including empty ones, and prove a gap-free cover.
        leaves = db.execute("SELECT * FROM _timestamp_ranges WHERE name=? AND status='complete' ORDER BY lower",(name,)).fetchall()
        edge = LOWER;source_total = 0
        for leaf in leaves:
            if leaf['lower'] != edge:
                raise ValueError('timestamp partition coverage gap or overlap')
            current = f.count_source(name,request,predicate(key,leaf['lower'],leaf['upper']))
            if current != leaf['source_count']:
                raise ValueError('timestamp source count drift; reconciliation required')
            source_total += current;edge = leaf['upper']
            if delay:time.sleep(delay)
        if edge != UPPER or outside_count(table,key,request):
            raise ValueError('timestamp partition source domain changed')
        actual = db.execute('SELECT count(*) FROM '+f.quote(name)).fetchone()[0]
        if actual != source_total:
            raise ValueError('timestamp snapshot count mismatch')
        db.execute("UPDATE _capture_state SET status='complete',expected_count=?,source_count=?,rows=?,offset=?,updated_at=?,error=NULL WHERE name=?",
                   (source_total,source_total,actual,actual,a.now(),name));db.commit()
    except Exception as error:
        db.rollback()
        db.execute("UPDATE _capture_state SET status='failed',error=?,updated_at=? WHERE name=?",(str(error),a.now(),name));db.commit()
        print('full timestamp metadata failed',name,type(error).__name__,str(error),flush=True)
