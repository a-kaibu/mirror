"""Repair drifting integer-keyed public tables using bounded group fingerprints."""
import hashlib
import json
import sqlite3
import time

import archiver as a
import erogamescape_full as f
import erogamescape_ranges as r

SCALARS={'smallint','integer','bigint','boolean','text','character varying','character',
         'date','timestamp with time zone','timestamp without time zone'}


def supported(table):
    pk=table.get('primary_key') or []
    columns={c['name']:c for c in table['columns']}
    return (bool(pk) and columns[pk[0]]['type'] in {'smallint','integer','bigint'}
            and all(c['type'] in SCALARS for c in columns.values()) and not f.exclusion(table))


def row_digest(table,row):
    values=[]
    for column in table['columns']:
        value=row[column['name']]
        if value is not None and column['type']=='boolean':
            if value not in (0,1):raise ValueError('invalid boolean in local capture')
            value=bool(value)
        values.append(value)
    return hashlib.md5(json.dumps(values,ensure_ascii=False,separators=(', ', ': ')).encode()).hexdigest()


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS _group_repair_state(name TEXT PRIMARY KEY,schema_sha256 TEXT,started_at TEXT);
      CREATE TABLE IF NOT EXISTS _group_repair_ranges(name TEXT,path TEXT,lower_key INTEGER,upper_key TEXT,status TEXT,
        PRIMARY KEY(name,path));
      CREATE TABLE IF NOT EXISTS _group_repair_pending(name TEXT PRIMARY KEY,group_key INTEGER,digest TEXT,rows INTEGER,cursor TEXT);
    """)


def active(db,name):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='_group_repair_state'").fetchone()
    return bool(exists and db.execute('SELECT 1 FROM _group_repair_state WHERE name=?',(name,)).fetchone())


def bounds(key,lower,upper):
    return [f.quote(key)+' >= '+str(lower),f.quote(key)+' < '+str(upper)]


def summary(table,lower,upper,request):
    pk=table['primary_key'];columns=','.join('src.'+f.quote(c['name']) for c in table['columns'])
    order=','.join('src.'+f.quote(k) for k in pk)
    predicate=bounds(pk[0],lower,upper)
    if f.public_filter(table):predicate.append(f.public_filter(table))
    sql=("SELECT json_build_object('id',1,'row',row_to_json(t))::text AS metadata FROM (SELECT src."
         +f.quote(pk[0])+" AS group_key,count(*) AS rows,md5(string_agg(md5(json_build_array("+columns+
         ")::text),'' ORDER BY "+order+")) AS digest FROM "+f.quote(table['table_name'])+
         ' src WHERE '+' AND '.join(predicate)+' GROUP BY src.'+f.quote(pk[0])+
         ' ORDER BY src.'+f.quote(pk[0])+' LIMIT 257) t')
    rows,response=f.transport(sql,request)
    previous=None
    for row in rows:
        key=row.get('group_key');size=row.get('rows');digest=row.get('digest')
        if (set(row)!={'group_key','rows','digest'} or type(key) is not int or not lower<=key<upper
                or (previous is not None and key<=previous) or type(size) is not int or size<=0
                or not isinstance(digest,str) or len(digest)!=32 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('invalid group fingerprint response')
        previous=key
    return rows,sql,response


def local_digest(db,table,target,key):
    pk=table['primary_key'];last=None;count=0;digest=hashlib.md5()
    while True:
        where=f.quote(pk[0])+'=?';params=[key]
        if last is not None:
            where+=' AND ('+','.join(map(f.quote,pk))+') > ('+','.join('?' for _ in pk)+')';params+=last
        cursor=db.execute('SELECT * FROM '+f.quote(target)+' WHERE '+where+' ORDER BY '+','.join(map(f.quote,pk))+' LIMIT 500',params)
        try:rows=cursor.fetchall()
        finally:cursor.close()
        if not rows:return count,digest.hexdigest()
        for row in rows:digest.update(row_digest(table,row).encode())
        count+=len(rows);last=[rows[-1][column] for column in pk]


def staging_name(name):return '_group_repair_rows_'+hashlib.sha256(name.encode()).hexdigest()


def repair_group(db,table,group,request,delay):
    name=table['table_name'];pk=table['primary_key'];key=group['group_key'];stage=staging_name(name)
    wanted=(group['rows'],group['digest'])
    if local_digest(db,table,name,key)==wanted:return
    pending=db.execute('SELECT * FROM _group_repair_pending WHERE name=?',(name,)).fetchone()
    if not pending or (pending['group_key'],pending['rows'],pending['digest'])!=(key,*wanted):
        db.execute('DELETE FROM '+f.quote(stage))
        db.execute('INSERT OR REPLACE INTO _group_repair_pending VALUES(?,?,?,?,NULL)',(name,key,group['digest'],group['rows']));db.commit()
    pending=db.execute('SELECT * FROM _group_repair_pending WHERE name=?',(name,)).fetchone()
    cursor=json.loads(pending['cursor']) if pending['cursor'] else None
    columns=[c['name'] for c in table['columns']]
    while True:
        predicates=[f.quote(pk[0])+' = '+f.literal(key)]
        if f.public_filter(table):predicates.append(f.public_filter(table))
        if cursor is not None:predicates.append('('+','.join(map(f.quote,pk))+') > ('+','.join(map(f.literal,cursor))+')')
        sql="SELECT json_build_object('id',1,'row',row_to_json(t))::text AS metadata FROM (SELECT src.* FROM "+f.quote(name)+' src WHERE '+' AND '.join(predicates)+' ORDER BY '+','.join(map(f.quote,pk))+' LIMIT '+str(f.capture_batch_size(table))+') t'
        if db.in_transaction:raise ValueError('repair request inside SQLite transaction')
        rows,response=f.transport(sql,request)
        if rows:
            if any(set(row)!=set(columns) or row[pk[0]]!=key for row in rows):raise ValueError('group body schema/key mismatch')
            next_cursor=[rows[-1][k] for k in pk]
            if next_cursor==cursor:raise ValueError('group body cursor did not advance')
            db.executemany('INSERT INTO '+f.quote(stage)+' VALUES('+','.join('?' for _ in columns)+')',
                           ([f.cell(row[c]) for c in columns] for row in rows))
            db.execute('UPDATE _group_repair_pending SET cursor=? WHERE name=?',(json.dumps(next_cursor),name));cursor=next_cursor
        db.execute('INSERT INTO _capture_requests(name,sql,response_sha256,row_count,fetched_at) VALUES(?,?,?,?,?)',(name,sql,response,len(rows),a.now()));db.commit()
        if not rows:break
        time.sleep(delay)
    if local_digest(db,table,stage,key)!=wanted:
        db.execute('DELETE FROM '+f.quote(stage));db.execute('DELETE FROM _group_repair_pending WHERE name=?',(name,));db.commit()
        raise ValueError('source group changed during repair')
    with db:
        before=db.execute('SELECT count(*) FROM '+f.quote(name)+' WHERE '+f.quote(pk[0])+'=?',(key,)).fetchone()[0]
        db.execute('DELETE FROM '+f.quote(name)+' WHERE '+f.quote(pk[0])+'=?',(key,))
        db.execute('INSERT INTO '+f.quote(name)+' SELECT * FROM '+f.quote(stage))
        db.execute('DELETE FROM '+f.quote(stage));db.execute('DELETE FROM _group_repair_pending WHERE name=?',(name,))
        db.execute('UPDATE _capture_state SET rows=rows+?,offset=offset+?,updated_at=? WHERE name=?',(group['rows']-before,group['rows']-before,a.now(),name))
    print('full metadata group repaired',name,'key',key,'rows',group['rows'],flush=True)


def repair(db,table,request=f.e.query,delay=3,max_pages=None):
    name=table['table_name']
    if not supported(table):raise ValueError('group fingerprint repair does not support this schema')
    initialize(db);pk=table['primary_key'];key,kind,lower,upper=r.key_domain(table,request)
    # Local SQLite text ordering must match all source pagination dimensions.
    for index in range(1,len(pk)):r.next_domain(table,index,request)
    fingerprint=hashlib.sha256(json.dumps(table,sort_keys=True).encode()).hexdigest()
    state=db.execute('SELECT * FROM _group_repair_state WHERE name=?',(name,)).fetchone()
    if state and state['schema_sha256']!=fingerprint:raise ValueError('group repair source schema changed')
    stage=staging_name(name)
    if not state:
        definition=','.join(f.quote(c['name'])+' '+f.sqlite_type(c) for c in table['columns'])+',PRIMARY KEY('+','.join(map(f.quote,pk))+')'
        db.execute('CREATE TABLE '+f.quote(stage)+'('+definition+')')
        db.execute('INSERT INTO _group_repair_state VALUES(?,?,?)',(name,fingerprint,a.now()))
        db.execute("INSERT INTO _group_repair_ranges VALUES(?,?,?,?,'pending')",(name,'',lower,str(upper)));db.commit()
    db.execute("UPDATE _capture_state SET status='running',error=NULL,source_count=NULL,updated_at=? WHERE name=?",(a.now(),name));db.commit()
    pages=0
    try:
        while max_pages is None or pages<max_pages:
            segment=db.execute("SELECT * FROM _group_repair_ranges WHERE name=? AND status='pending' ORDER BY path LIMIT 1",(name,)).fetchone()
            if segment is None:
                actual=db.execute('SELECT count(*) FROM '+f.quote(name)).fetchone()[0]
                try:source=f.count_source(name,request,f.public_filter(table))
                except f.QueryCostExceeded:source=r.bounded_count(db,table,request,key,kind,lower,upper)
                if source!=actual:
                    db.execute("UPDATE _group_repair_ranges SET status='pending' WHERE name=? AND status='complete'",(name,));db.commit()
                    raise ValueError('final source count changed; recheck summaries')
                with db:
                    db.execute("UPDATE _capture_state SET status='complete',rows=?,offset=?,source_count=?,expected_count=?,updated_at=?,error=NULL WHERE name=?",(actual,actual,source,source,a.now(),name))
                    started=db.execute('SELECT started_at FROM _group_repair_state WHERE name=?',(name,)).fetchone()[0]
                    receipt={'rows':actual,'source_count':source,'schema_sha256':fingerprint,'started_at':started,'verified_at':a.now(),'method':'ordered full-row group fingerprints'}
                    db.execute('INSERT OR REPLACE INTO _archive_info VALUES(?,?)',('reconciliation_final:'+name,json.dumps(receipt)))
                    db.execute('DELETE FROM _group_repair_state WHERE name=?',(name,));db.execute('DELETE FROM _group_repair_ranges WHERE name=?',(name,))
                    db.execute('DELETE FROM _group_repair_pending WHERE name=?',(name,));db.execute('DROP TABLE '+f.quote(stage))
                print('full metadata group repair complete',name,actual,flush=True);return
            lo,hi=segment['lower_key'],int(segment['upper_key']);path=segment['path']
            try:
                groups,sql,response=summary(table,lo,hi,request)
                pivot=groups[128]['group_key'] if len(groups)==257 else None
            except f.QueryCostExceeded:
                if hi-lo<=1:raise
                pivot=(lo+hi)//2;groups=None
            if pivot is not None:
                if not lo<pivot<hi:raise ValueError('group summary range did not split')
                with db:
                    db.execute("UPDATE _group_repair_ranges SET status='split' WHERE name=? AND path=?",(name,path))
                    for suffix,left,right in [('0',lo,pivot),('1',pivot,hi)]:
                        db.execute("INSERT INTO _group_repair_ranges VALUES(?,?,?,?,'pending')",(name,path+suffix,left,str(right)))
                pages+=1;time.sleep(delay);continue
            keys={g['group_key'] for g in groups};last=None
            while True:
                predicates=bounds(key,lo,hi);params=[]
                if last is not None:predicates.append(f.quote(key)+'>?');params.append(last)
                local=db.execute('SELECT DISTINCT '+f.quote(key)+' FROM '+f.quote(name)+' WHERE '+' AND '.join(predicates)+' ORDER BY '+f.quote(key)+' LIMIT 256',params).fetchall()
                if not local:break
                for row in local:
                    value=row[0]
                    if value not in keys:repair_group(db,table,{'group_key':value,'rows':0,'digest':hashlib.md5(b'').hexdigest()},request,delay)
                last=local[-1][0]
            for group in groups:repair_group(db,table,group,request,delay)
            after,_,_=summary(table,lo,hi,request)
            if after!=groups:raise ValueError('source range changed during repair')
            with db:
                db.execute("UPDATE _group_repair_ranges SET status='complete' WHERE name=? AND path=?",(name,path))
                db.execute('INSERT INTO _capture_requests(name,sql,response_sha256,row_count,fetched_at) VALUES(?,?,?,?,?)',(name,sql,response,len(groups),a.now()))
            print('full metadata group range verified',name,lo,hi,'groups',len(groups),flush=True)
            pages+=1;time.sleep(delay)
    except Exception as error:
        db.rollback()
        db.execute("UPDATE _capture_state SET status='failed',updated_at=?,error=? WHERE name=?",(a.now(),'range repair: '+str(error),name));db.commit()
        print('full metadata group repair deferred',name,type(error).__name__,flush=True)
