"""Recover observed file URLs from archived download-CGI redirects."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode,urljoin,urlsplit
from urllib.request import HTTPRedirectHandler,build_opener
import archiver as a
import mirror_sources as m
from archive_lock import exclusive_file

REDIRECTS={301,302,303,307,308}
PARSER_SHA=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def captures(payload,target,scheme='https'):
    if not isinstance(payload,list):raise ValueError('CDX response is not an array')
    if not payload:return []
    required={'timestamp','original','statuscode','digest'}
    if not isinstance(payload[0],list) or not required<=set(payload[0]):raise ValueError('CDX columns missing')
    result=[]
    for values in payload[1:]:
        if not isinstance(values,list) or len(values)!=len(payload[0]):raise ValueError('CDX row has unexpected columns')
        row=dict(zip(payload[0],values))
        try:identity=a.archive_url_identity(row['original'])
        except ValueError:continue
        if identity!=a.archive_url_identity(target):continue
        if str(row['statuscode']) not in {str(v) for v in REDIRECTS} or not re.fullmatch(r'\d{14}',row['timestamp']):continue
        # Redirect bodies can be identical while Location changes. Do not
        # collapse these observations by the body digest.
        result.append(a.replay(row['original'],row['timestamp'],raw=True,scheme=scheme))
    return list(dict.fromkeys(result))


def initialize(store):
    store.db.executescript("""
      CREATE TABLE IF NOT EXISTS redirect_lookups(url TEXT PRIMARY KEY,queried_at TEXT,status TEXT,data TEXT,error TEXT);
      CREATE TABLE IF NOT EXISTS redirect_observations(url TEXT PRIMARY KEY,checked_at TEXT,status INTEGER,location TEXT,response_url TEXT);
      CREATE TABLE IF NOT EXISTS redirect_scans(item_id TEXT,url TEXT,expected_md5 TEXT,expected_sha256 TEXT,parser_sha TEXT,status TEXT,checked_at TEXT,PRIMARY KEY(item_id,url));
    """)


def lookup(store,fetcher,target):
    saved=store.db.execute("SELECT data FROM redirect_lookups WHERE url=? AND status='ok'",(target,)).fetchone()
    if saved:return captures(json.loads(saved['data']),target,scheme=a.archive_index_scheme(store,target))
    endpoint='https://web.archive.org/cdx/search/cdx?'+urlencode({'url':target,'output':'json','filter':'statuscode:(301|302|303|307|308)','fl':'timestamp,original,mimetype,statuscode,digest,length','limit':10000})
    with a.open_archive_index(fetcher,endpoint) as response:raw=response.read(16*1024*1024+1);actual=response.geturl()
    if len(raw)>16*1024*1024:raise ValueError('CDX response exceeds bound')
    payload=json.loads(raw)
    if isinstance(payload,list) and len(payload)>=10001:raise ValueError('CDX truncation requires pagination')
    urls=captures(payload,target,scheme=a.archive_index_scheme(store,target,actual))
    store.db.execute('INSERT OR REPLACE INTO redirect_lookups VALUES(?,?,?,?,?)',(target,a.now(),'ok',json.dumps(payload),None));store.db.commit()
    return urls


def observe(store,fetcher,url):
    saved=store.db.execute('SELECT * FROM redirect_observations WHERE url=?',(url,)).fetchone()
    if saved:return dict(saved)
    try:
        with a.open_archive_page(fetcher,url) as response:
            status=response.status;location=response.headers.get('Location');actual=response.geturl()
    except HTTPError as error:
        if error.code not in REDIRECTS:error.close();raise
        status=error.code;location=error.headers.get('Location');actual=error.geturl();error.close()
    # An availability/transport redirect may precede the publisher's response.
    if not a.WAYBACK.match(actual):raise ValueError('redirect observation left Wayback')
    if status not in REDIRECTS and status!=200:raise ValueError('unexpected replay status')
    observation={'url':url,'checked_at':a.now(),'status':status,'location':location,'response_url':actual}
    store.db.execute('INSERT OR REPLACE INTO redirect_observations VALUES(:url,:checked_at,:status,:location,:response_url)',observation);store.db.commit()
    return observation


def file_target(observation):
    location=observation.get('location')
    if observation['status'] not in REDIRECTS or not location:return None
    if any(ord(c)<32 or ord(c)==127 for c in location):return None
    base=observation['response_url']
    resolved=urljoin(base,location) if location.startswith(('/web/','http://','https://')) else urljoin(a.original(base),location)
    target=a.original(resolved)
    try:a.archive_url_identity(target)
    except ValueError:return None
    return target if a.EXT.search(urlsplit(target).path) else None


def follow(store,fetcher,capture):
    seen=set();url=capture
    for _ in range(5):
        if url in seen:raise ValueError('archived redirect cycle')
        seen.add(url);observation=observe(store,fetcher,url)
        yield observation
        if file_target(observation) or observation['status'] not in REDIRECTS:return
        location=observation.get('location') or ''
        target=urljoin(observation['response_url'],location)
        # Follow only an actual replay URL returned in Location, without
        # inventing a capture for a different original URL.
        if not a.WAYBACK.match(target):return
        url=target
    raise ValueError('archived redirect chain exceeds bound')


def add_observations(store,ident,snapshot,observations):
    files=[(o,file_target(o)) for o in observations if file_target(o)]
    if not files:return 0
    with exclusive_file(store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock'),blocking=False):
        row=store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()
        if not row:return 0
        record=json.loads(row['data'])
        if (record.get('expected_md5'),record.get('expected_sha256'))!=(snapshot.get('expected_md5'),snapshot.get('expected_sha256')):return 0
        if not (record.get('expected_md5') or record.get('expected_sha256')):return 0
        fields=record.setdefault('fields',{});evidence=fields.setdefault('redirect_evidence',[]);added=0
        for observation,target in files:
            if target not in record['mirrors']:record['mirrors'].append(target);added+=1
            fields['recovery_urls']=list(dict.fromkeys(fields.get('recovery_urls',[])+[target]))
            if not any(e.get('request_url')==observation['url'] for e in evidence):
                evidence.append({'request_url':observation['url'],'response_url':observation['response_url'],'status':observation['status'],'location':observation['location'],'original_target':target,'observed_at':observation['checked_at']})
        # Only the original target is added. A replay timestamp from the CGI
        # does not prove that the binary was captured on that date.
        m.add_record(store,record)
        if added:store.db.execute("UPDATE items SET status='pending' WHERE id=? AND status='failed'",(ident,));store.db.commit()
        return added


def collect(store,index_fetcher,replay_fetcher,site='d_station',limit=100):
    initialize(store);processed=added=failed=0
    for row in store.rows(site):
        if row['status'] not in {'pending','failed'}:continue
        record=json.loads(row['data'])
        if not (record.get('expected_md5') or record.get('expected_sha256')):continue
        targets=list(dict.fromkeys(a.original(u) for u in record['mirrors'] if urlsplit(a.original(u)).path.lower().endswith(('.php','.cgi','.asp','.aspx'))))
        for target in targets:
            saved=store.db.execute("SELECT * FROM redirect_scans WHERE item_id=? AND url=?",(row['id'],target)).fetchone()
            signature=(record.get('expected_md5'),record.get('expected_sha256'),PARSER_SHA)
            if saved and saved['status']=='ok' and (saved['expected_md5'],saved['expected_sha256'],saved['parser_sha'])==signature:continue
            if processed>=limit:return processed,added,failed
            processed+=1;observations=[]
            try:
                for capture in lookup(store,index_fetcher,target):
                    observations.extend(follow(store,replay_fetcher,capture))
                count=add_observations(store,row['id'],record,observations);added+=count
                store.db.execute('INSERT OR REPLACE INTO redirect_scans VALUES(?,?,?,?,?,?,?)',(row['id'],target,*signature,'ok',a.now()));store.db.commit()
                print('redirect recovery',target,'observations',len(observations),'new file URLs',count,flush=True)
            except BlockingIOError:
                print('redirect recovery deferred busy record',row['id'],flush=True)
            except (OSError,ValueError) as error:
                try:added+=add_observations(store,row['id'],record,observations)
                except BlockingIOError:print('redirect recovery deferred busy record',row['id'],flush=True)
                failed+=1
                print('redirect recovery failed',target,error,flush=True)
    return processed,added,failed


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--site',default='d_station');parser.add_argument('--limit',type=a.positive,default=100);args=parser.parse_args()
    with exclusive_file(Path(args.root)/'.claims'/'redirect-recovery.lock'):
        store=a.Store(args.root)
        try:
            replay=a.Fetcher(delay=10,timeout=30,retries=2);replay.opener=build_opener(NoRedirect())
            print('redirect recovery result',collect(store,a.Fetcher(delay=10,timeout=30,retries=2),replay,args.site,args.limit),flush=True)
        finally:store.db.close()
