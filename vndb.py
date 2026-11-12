"""Convert the complete official VNDB public dump to a standalone SQLite file."""
import argparse
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import archiver as a


def quote(value):
    return '"'+value.replace('"','""')+'"'


def schema(sql):
    tables={}
    for name,body in re.findall(r'CREATE TABLE ([a-z_]+) \((.*?)\n\);',sql,re.S):
        columns=[];primary=[]
        for line in body.splitlines():
            line=line.strip().rstrip(',')
            if not line:continue
            if line.startswith('PRIMARY KEY('):primary=[v.strip() for v in line[12:-1].split(',')];continue
            match=re.fullmatch(r'([a-z_]+) (.+)',line)
            if not match:raise ValueError('unsupported schema: '+line)
            column,definition=match.groups()
            datatype=re.split(r' NOT NULL| CHECK\(',definition)[0].strip()
            columns.append((column,datatype,'NOT NULL' in definition))
        if not columns or name in tables:raise ValueError('invalid table '+name)
        tables[name]=(columns,primary)
    if not tables:raise ValueError('no VNDB schema tables')
    return tables


def unescape(value):
    if value==b'\\N':return None
    replacements={ord('b'):8,ord('f'):12,ord('n'):10,ord('r'):13,ord('t'):9,ord('v'):11}
    out=bytearray();index=0
    while index<len(value):
        ch=value[index];index+=1
        if ch!=92:out.append(ch);continue
        if index==len(value):raise ValueError('trailing COPY escape')
        ch=value[index];index+=1
        if ch==ord('x'):
            end=index
            while end<min(index+2,len(value)) and value[end] in b'0123456789abcdefABCDEF':end+=1
            if end==index:raise ValueError('empty hex escape')
            out.append(int(value[index:end],16));index=end
        elif ch in b'01234567':
            digits=bytes([ch])
            while len(digits)<3 and index<len(value) and value[index] in b'01234567':digits+=value[index:index+1];index+=1
            out.append(int(digits,8))
        else:out.append(replacements.get(ch,ch))
    return out.decode('utf-8',errors='strict')


def sqlite_type(datatype):
    return 'INTEGER' if datatype in {'smallint','integer','bigint','boolean'} else 'TEXT'


def convert(source,target,source_url=None):
    source=Path(source);target=Path(target);temp=target.with_suffix('.building.sqlite3')
    target.parent.mkdir(parents=True,exist_ok=True);temp.unlink(missing_ok=True)
    db=sqlite3.connect(temp)
    process=subprocess.Popen(['zstd','-dc',str(source)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    tables=None;seen=set();headers=set();documents={}
    try:
        db.execute('CREATE TABLE _archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('CREATE TABLE _source_tables(name TEXT PRIMARY KEY,columns_json TEXT,rows INTEGER)')
        db.execute('CREATE TABLE _source_documents(name TEXT PRIMARY KEY,content TEXT)')
        with tarfile.open(fileobj=process.stdout,mode='r|') as archive:
            for member in archive:
                if not member.isfile():continue
                name=member.name
                if name.startswith('db/') and name.endswith('.header'):
                    table=name[3:-7]
                    if tables is None or table not in tables or table in headers:raise ValueError('unexpected COPY header '+name)
                    if member.size>65536:raise ValueError('oversized COPY header')
                    raw=archive.extractfile(member).read()
                    columns=raw.decode('utf-8').rstrip('\n').split('\t')
                    if columns!=[column[0] for column in tables[table][0]]:raise ValueError('COPY header/schema mismatch '+table)
                    headers.add(table);db.execute('INSERT INTO _source_documents VALUES(?,?)',(name,raw.decode('utf-8')))
                elif name.startswith('db/'):
                    table=name[3:]
                    if tables is None or table not in tables or table in seen:raise ValueError('unexpected table '+name)
                    columns,primary=tables[table];seen.add(table)
                    definitions=[quote(n)+' '+sqlite_type(t)+(' NOT NULL' if required else '') for n,t,required in columns]
                    if primary:definitions.append('PRIMARY KEY('+','.join(map(quote,primary))+')')
                    db.execute('CREATE TABLE '+quote(table)+'('+','.join(definitions)+')')
                    insert='INSERT INTO '+quote(table)+' VALUES('+','.join('?' for _ in columns)+')'
                    batch=[];count=0
                    with archive.extractfile(member) as stream:
                        for line in stream:
                            if not line.endswith(b'\n'):raise ValueError('unterminated COPY row '+table)
                            values=line[:-1].split(b'\t')
                            if len(values)!=len(columns):raise ValueError('COPY column mismatch '+table)
                            row=[]
                            for raw,(_,datatype,_) in zip(values,columns):
                                value=unescape(raw)
                                if value is not None and datatype=='boolean':
                                    if value not in {'t','f'}:raise ValueError('invalid boolean')
                                    value=int(value=='t')
                                elif value is not None and sqlite_type(datatype)=='INTEGER':value=int(value)
                                row.append(value)
                            batch.append(row);count+=1
                            if len(batch)==5000:db.executemany(insert,batch);batch=[]
                    if batch:db.executemany(insert,batch)
                    db.execute('INSERT INTO _source_tables VALUES(?,?,?)',(table,json.dumps(columns),count));db.commit()
                    print('VNDB',table,count,flush=True)
                else:
                    if member.size>2*1024*1024:raise ValueError('oversized metadata '+name)
                    raw=archive.extractfile(member).read();text=raw.decode('utf-8')
                    documents[name]=text;db.execute('INSERT INTO _source_documents VALUES(?,?)',(name,text))
                    if name=='schema_plain.sql':tables=schema(text)
        process.stdout.close();errors=process.stderr.read();code=process.wait()
        if code:raise ValueError('zstd failed: '+errors.decode(errors='replace')[:500])
        if tables is None or seen!=set(tables):raise ValueError('dump missing tables: '+str(set(tables or {})-seen))
        if headers!=set(tables):raise ValueError('dump missing COPY headers: '+str(set(tables)-headers))
        for name,count in db.execute('SELECT name,rows FROM _source_tables').fetchall():
            if db.execute('SELECT count(*) FROM '+quote(name)).fetchone()[0]!=count:raise ValueError('row count mismatch')
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity failed')
        info={'source_url':source_url or source.name,'source_sha256':a.digest(source)['sha256'],'dump_timestamp':documents.get('TIMESTAMP','').strip(),'converted_at':a.now(),'complete':True,'tables':len(seen),'format':'PostgreSQL COPY; arrays/enums retained as source text; boolean stored as 0/1'}
        db.executemany('INSERT INTO _archive_info VALUES(?,?)',[(k,json.dumps(v,ensure_ascii=False)) for k,v in info.items()]);db.commit();db.close();temp.replace(target)
        return target
    except BaseException:
        db.close();temp.unlink(missing_ok=True);raise
    finally:
        if process.poll() is None:process.terminate();process.wait()
        if process.stdout:process.stdout.close()
        if process.stderr:process.stderr.close()


def publish(path):
    expected=a.digest(path);client=a.s3_client('vn-archive','hf')
    client.upload_file(str(path),'mirrors','vndb.sqlite3',ExtraArgs={'ContentType':'application/vnd.sqlite3','Metadata':{'sha256':expected['sha256']}})
    if a.public_digest('vn-archive','mirrors','vndb.sqlite3')!=(expected['sha256'],expected['size']):raise ValueError('VNDB public read-back mismatch')
    print('VNDB publicly verified',expected['sha256'],expected['size'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('dump');parser.add_argument('--output',default='archive/vndb.sqlite3');parser.add_argument('--source-url');parser.add_argument('--upload',action='store_true')
    args=parser.parse_args();path=convert(args.dump,args.output,args.source_url)
    if args.upload:publish(path)
