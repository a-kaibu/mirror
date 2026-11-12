"""Queue historical HTML captures, including captures before mirror closure."""
import argparse
import json
import re
from urllib.parse import urlencode,urlsplit
import archiver as a
import mirror_sources as m


def history(payload,target,scheme="https"):
    if not isinstance(payload,list):raise ValueError('CDX response must be an array')
    if not payload:return []
    if not isinstance(payload[0],list) or not {'timestamp','original','statuscode','digest'}<=set(payload[0]):raise ValueError('CDX columns missing')
    base=urlsplit(m.clean(target));result=[];seen=set()
    for values in payload[1:]:
        if not isinstance(values,list) or len(values)!=len(payload[0]):raise ValueError('invalid CDX row')
        row=dict(zip(payload[0],values));url=urlsplit(m.clean(row['original']))
        if (url.hostname,url.path,url.query)!=(base.hostname,base.path,base.query):continue
        if url.scheme not in {'http','https'} or str(row['statuscode'])!='200' or not re.fullmatch(r'\d{14}',row['timestamp']):continue
        if row.get('mimetype') not in {'text/html','application/xhtml+xml'}:continue
        # Identical bodies at many dates add no catalogue information. Preserve
        # every distinct body and its earliest actual capture timestamp.
        key=row['digest'] if row['digest']!='-' else row['timestamp']
        if key in seen:continue
        seen.add(key);result.append(a.replay(row['original'],row['timestamp'],scheme=scheme))
    return result


def discover(store,fetcher,source):
    m.initialize(store)
    store.db.execute('CREATE TABLE IF NOT EXISTS mirror_history(site TEXT,url TEXT,checked_at TEXT,status TEXT,data TEXT,error TEXT,PRIMARY KEY(site,url))');store.db.commit()
    queued=0
    for url in dict.fromkeys(source['seeds']+source['homes']):
        scheme=a.archive_index_scheme(store,url)
        cached=store.db.execute('SELECT status,data FROM mirror_history WHERE site=? AND url=?',(source['key'],url)).fetchone()
        try:
            if cached and cached['status']=='ok':payload=json.loads(cached['data'])
            else:
                endpoint='https://web.archive.org/cdx/search/cdx?'+urlencode({'url':url,'output':'json','filter':'statuscode:200','fl':'timestamp,original,mimetype,statuscode,digest','limit':10000})
                with a.open_archive_index(fetcher,endpoint) as response:
                    raw=response.read(16*1024*1024+1);index_url=response.geturl()
                if len(raw)>16*1024*1024:raise ValueError('CDX response exceeds bound')
                payload=json.loads(raw)
                if isinstance(payload,list) and len(payload)>=10001:raise ValueError('CDX truncation requires pagination')
                history(payload,url)
                scheme=a.archive_index_scheme(store,url,index_url)
                store.db.execute('INSERT OR REPLACE INTO mirror_history VALUES(?,?,?,?,?,?)',(source['key'],url,a.now(),'ok',json.dumps(payload),None))
            captures=history(payload,url,scheme=scheme)
            for capture in captures:
                if not m.allowed(source,capture):raise ValueError('capture outside registered hosts')
                queued+=store.db.execute("INSERT OR IGNORE INTO mirror_frontier(site,url,mode) VALUES(?,?,'archive')",(source['key'],capture)).rowcount
            store.db.commit();print('mirror history',source['key'],url,len(captures),'distinct captures',flush=True)
        except (OSError,ValueError) as error:
            store.db.execute('INSERT OR REPLACE INTO mirror_history VALUES(?,?,?,?,?,?)',(source['key'],url,a.now(),'failed',None,str(error)));store.db.commit();print('mirror history failed',url,error,flush=True)
    return queued


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--site',required=True,choices=list(m.definitions()))
    args=parser.parse_args();store=a.Store(args.root)
    try:print('queued',discover(store,a.Fetcher(delay=10,timeout=30,retries=2),m.definitions()[args.site]))
    finally:store.db.close()
