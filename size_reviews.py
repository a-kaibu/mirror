"""Keep evidence-backed size corrections in the authoritative SQLite catalog."""
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS size_reviews(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
        original_metadata TEXT NOT NULL,correction TEXT NOT NULL,evidence TEXT NOT NULL,reviewed_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS size_review_item ON size_reviews(item_id,fingerprint);
      CREATE TABLE IF NOT EXISTS artifact_observations(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,source_url TEXT NOT NULL,
        path TEXT NOT NULL,size INTEGER NOT NULL,md5 TEXT NOT NULL,sha256 TEXT NOT NULL,observed_at TEXT NOT NULL,evidence TEXT NOT NULL);
    """)


def page_digest(store, record):
    import archiver as a
    row=store.db.execute("SELECT path FROM pages WHERE url=? AND status='ok'",(record['page_url'],)).fetchone()
    if not row or not row[0] or not (store.root/row[0]).is_file():
        raise ValueError('saved source HTML is required for a size review')
    return a.digest(store.root/row[0])['sha256']


def metadata(record, html_hash):
    return {**{k:record.get(k) for k in ['id','site','page_url','original_page_url','title','brand','filename','expected_size','expected_md5','expected_sha256']},
            'mirrors':sorted(record.get('mirrors',[])), 'source_html_sha256':html_hash}


def fingerprint(values):
    return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def apply(store, record):
    rows=store.db.execute('SELECT * FROM size_reviews WHERE item_id=? ORDER BY reviewed_at DESC,id DESC',(record['id'],)).fetchall()
    if not rows:return record
    try:stamp=fingerprint(metadata(record,page_digest(store,record)))
    except (OSError,ValueError):return record
    for row in rows:
        if row['fingerprint']!=stamp:continue
        correction=json.loads(row['correction'])
        if set(correction)!={'expected_size','expected_sha256'} or type(correction['expected_size']) is not int or correction['expected_size']<=0:
            raise ValueError('invalid stored size correction')
        import re
        if not re.fullmatch(r'[a-f0-9]{64}',correction['expected_sha256']):raise ValueError('invalid stored review checksum')
        return {**record,**correction,'fields':{**record.get('fields',{}),
            'publisher_expected_size':record.get('expected_size'),
            'size_review':{'id':row['id'],'reviewed_at':row['reviewed_at'],
                           'basis':'matching full historical copy and ZIP CRC',
                           'evidence':json.loads(row['evidence'])}}}
    return record


def review(store, record, current_path, historical_path, capture):
    """Correct a size only after independently fetching an identical archived ZIP."""
    import archiver as a
    import mirror_sources as m
    if Path(current_path).resolve()==Path(historical_path).resolve():raise ValueError('two independent copies are required')
    if not str(record.get('filename','')).lower().endswith('.zip'):raise ValueError('size review currently requires a ZIP archive')
    current=a.digest(current_path);historical=a.digest(historical_path)
    if not current['size'] or current!=historical:raise ValueError('historical copy differs from observed source file')
    if record.get('expected_size') is None or record['expected_size']==current['size']:
        raise ValueError('there is no published size discrepancy')
    if record.get('expected_md5') and record['expected_md5']!=current['md5']:
        raise ValueError('publisher MD5 contradicts the observed file')
    if record.get('expected_sha256') and record['expected_sha256']!=current['sha256']:
        raise ValueError('publisher SHA-256 contradicts the observed file')
    import re
    match=a.WAYBACK.match(capture.get('url',''))
    if not match or not re.fullmatch(r'\d{14}',capture.get('captured_at','')) or match[1]!=capture.get('captured_at'):raise ValueError('invalid historical capture identity')
    def identity(url):
        parts=urlsplit(a.original(url))
        return parts.hostname,parts.path,parts.query
    if identity(capture['url'])!=identity(capture.get('original_url','')) or identity(capture['url']) not in {identity(u) for u in record['mirrors']}:
        raise ValueError('historical copy belongs to another source URL')
    with Path(current_path).open('rb') as stream:
        sha1=hashlib.file_digest(stream,'sha1').digest()
    if base64.b32encode(sha1).decode()!=capture.get('index_digest'):
        raise ValueError('historical index digest differs from the complete file')
    with zipfile.ZipFile(current_path) as archive:
        entries=archive.infolist();uncompressed=sum(i.file_size for i in entries)
        if len(entries)>10000 or uncompressed>128*1024*1024:raise ValueError('ZIP review decompression bound exceeded')
        if archive.testzip() is not None:raise ValueError('ZIP CRC failed')
    original=metadata(record,page_digest(store,record));stamp=fingerprint(original)
    correction={'expected_size':current['size'],'expected_sha256':current['sha256']}
    evidence={'capture':capture,'observed':current,'historical':historical,'zip_crc':'ok',
              'uncompressed_size':uncompressed,'members':len(entries),'source_html_sha256':original['source_html_sha256']}
    ident=hashlib.sha256(('size-review\n'+record['id']+'\n'+stamp+'\n'+current['sha256']+'\n'+capture['url']).encode()).hexdigest()
    try:
        store.db.execute('BEGIN IMMEDIATE')
        row=store.db.execute('SELECT data FROM items WHERE id=?',(record['id'],)).fetchone()
        if not row or json.loads(row[0])!=record:raise ValueError('source metadata changed during review')
        store.db.execute('INSERT OR IGNORE INTO size_reviews VALUES(?,?,?,?,?,?,?)',
            (ident,record['id'],stamp,json.dumps(original,ensure_ascii=False),json.dumps(correction),json.dumps(evidence,ensure_ascii=False),a.now()))
        result=m.add_record(store,dict(record))
        return result
    except Exception:
        store.db.rollback();raise


def observe(store, record, path, url, evidence):
    import shutil
    import archiver as a
    result=a.digest(path);rel='observations/'+result['sha256'];dest=store.root/rel
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():shutil.copyfile(path,dest)
    if a.digest(dest)!=result:raise ValueError('observation copy mismatch')
    ident=hashlib.sha256((record['id']+'\n'+url+'\n'+result['sha256']).encode()).hexdigest()
    store.db.execute('INSERT OR REPLACE INTO artifact_observations VALUES(?,?,?,?,?,?,?,?,?)',
        (ident,record['id'],url,rel,result['size'],result['md5'],result['sha256'],a.now(),json.dumps(evidence,ensure_ascii=False)))
    store.db.commit();return dest,result


def fetch_copy(fetcher,url,dest,referer=None):
    import archiver as a
    with fetcher.open(url,{'Referer':referer} if referer else None) as response:
        raw=response.read(64*1024*1024+1);actual=response.geturl()
        length=response.headers.get('Content-Length')
    if len(raw)>64*1024*1024:raise ValueError('size review download bound exceeded')
    if length is not None and int(length)!=len(raw):raise ValueError('review transfer length mismatch')
    dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
    a.validate(dest,{})
    return actual


def collect(store,fetcher,site='alice_archives',limit=None,publish=False):
    import re
    import shutil
    import http.client
    import archiver as a
    from archive_lock import exclusive_file
    from bulk import publicly_archived,evict_verified
    from urllib.error import URLError
    from urllib.parse import urlencode
    from wayback_recovery import captures
    if publish:
        pending=store.db.execute("""SELECT i.* FROM items i JOIN size_reviews r ON r.id=json_extract(i.data,'$.fields.size_review.id')
            WHERE (?='both' OR i.site=?) AND i.status='verified'""",(site,site)).fetchall()
        for pending_row in pending:
            with exclusive_file(store.root/'.claims'/(hashlib.sha256(pending_row['id'].encode()).hexdigest()+'.lock')):
                active=store.db.execute('SELECT * FROM items WHERE id=?',(pending_row['id'],)).fetchone()
                if publicly_archived(active,'vn-archive','mirrors'):continue
                if active['status']!='verified' or not active['path']:continue
                a.validate(store.root/active['path'],json.loads(active['data']),dict(active))
                a.publish(store,'vn-archive','mirrors','hf',active['site'],ident=active['id'],metadata=False)
                evict_verified(store,active['id'],'vn-archive','mirrors')
    rows=store.db.execute("""SELECT * FROM items WHERE (?='both' OR site=?) AND status='failed'
        AND error='published file size mismatch' ORDER BY id""",(site,site)).fetchall()
    processed=reviewed=unresolved=0
    for row in rows:
        record=json.loads(row['data'])
        if not str(record.get('filename','')).lower().endswith('.zip'):continue
        if apply(store,record)!=record:continue
        if limit is not None and processed>=limit:break
        processed+=1;publication_started=False
        try:
            folder=store.root/'.work'/'size-reviews'/record['id'];folder.mkdir(parents=True,exist_ok=True)
            saved=store.db.execute('SELECT * FROM artifact_observations WHERE item_id=? AND source_url=? ORDER BY observed_at DESC LIMIT 1',(record['id'],record['mirrors'][0])).fetchone()
            if saved and (store.root/saved['path']).is_file():
                current=store.root/saved['path'];source=saved['source_url']
            else:
                current=folder/'source.zip';source=fetch_copy(fetcher,record['mirrors'][0],current,record['page_url'])
            current,digest=observe(store,record,current,source,{'published_size':record.get('expected_size'),'zip_review_pending':True})
            with current.open('rb') as stream:sha1=base64.b32encode(hashlib.file_digest(stream,'sha1').digest()).decode()
            endpoint='https://web.archive.org/cdx/search/cdx?'+urlencode({'url':a.original(record['mirrors'][0]),'output':'json',
                'filter':'statuscode:200','fl':'timestamp,original,mimetype,statuscode,digest,length','collapse':'digest','limit':'10000'})
            with a.open_archive_index(fetcher,endpoint) as response:raw=response.read(16*1024*1024+1)
            if len(raw)>16*1024*1024:raise ValueError('review index exceeds bound')
            payload=json.loads(raw)
            if len(payload)>=10001:raise ValueError('review index truncated')
            (folder/'cdx.json').write_bytes(raw)
            matches=[c for c in captures(payload,a.original(record['mirrors'][0])) if c['index_digest']==sha1]
            if not matches:raise ValueError('no independent historical digest matches the observed file')
            capture=min(matches,key=lambda c:c['captured_at']);historical=folder/'historical.zip'
            try:actual=fetch_copy(fetcher,capture['url'],historical)
            except (URLError,ConnectionRefusedError) as error:
                reason=error.reason if isinstance(error,URLError) else error
                if not isinstance(reason,ConnectionRefusedError):raise
                actual=fetch_copy(fetcher,capture['url'].replace('https://','http://',1),historical)
            if a.digest(historical)!=digest:raise ValueError('independent full copy hash mismatch')
            observe(store,record,historical,actual,{'historical_capture':capture,'current_sha256':digest['sha256']})
            ident=review(store,record,current,historical,capture);reviewed+=1
            print('size reviewed',record['filename'],'published',record['expected_size'],'observed',digest['size'],'capture',capture['captured_at'],flush=True)
            with exclusive_file(store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')):
                active=store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()
                if publicly_archived(active,'vn-archive','mirrors'):continue
                data=json.loads(active['data'])
                name=re.sub(r'[^\w.() -]','_',data['filename'])[:150].strip('. ') or 'download.bin'
                rel='files/'+active['site']+'/'+ident+'/'+name;dest=store.root/rel;dest.parent.mkdir(parents=True,exist_ok=True)
                temp=dest.with_suffix('.review-import');shutil.copyfile(current,temp)
                checked=a.validate(temp,data);temp.replace(dest)
                if not store.update_if_current(ident,active['data'],status='verified',path=rel,source_url=source,fetched_at=a.now(),error=None,**checked):
                    raise ValueError('source metadata changed during reviewed import')
                if publish:
                    publication_started=True
                    a.publish(store,'vn-archive','mirrors','hf',active['site'],ident=ident,metadata=False)
                    evict_verified(store,ident,'vn-archive','mirrors')
        except (OSError,ValueError,zipfile.BadZipFile,http.client.HTTPException,RuntimeError) as error:
            if publication_started:raise
            unresolved+=1;print('size review unresolved',record['filename'],error,flush=True)
    store.export();return {'processed':processed,'reviewed':reviewed,'unresolved':unresolved}


if __name__=='__main__':
    import argparse
    import archiver as a
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive');parser.add_argument('--site',default='alice_archives')
    parser.add_argument('--limit',type=a.positive);parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    store=a.Store(args.root)
    try:print('size reviews',collect(store,a.Fetcher(delay=10,timeout=30,retries=1),args.site,args.limit,args.publish),flush=True)
    finally:store.db.close()
