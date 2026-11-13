"""Queue historical HTML captures, including captures before mirror closure."""
import argparse
import json
import re
from pathlib import Path
import subprocess
import sys
import time
from archive_lock import exclusive_file
from urllib.parse import urlencode,urlsplit
import archiver as a
import mirror_sources as m


def history(payload,target,scheme="https"):
    if not isinstance(payload,list):raise ValueError('CDX response must be an array')
    if not payload:return []
    if not isinstance(payload[0],list) or not {'timestamp','original','statuscode','digest'}<=set(payload[0]):raise ValueError('CDX columns missing')
    base=a.archive_url_identity(m.clean(target));result=[];seen=set()
    for values in payload[1:]:
        if not isinstance(values,list) or len(values)!=len(payload[0]):raise ValueError('invalid CDX row')
        row=dict(zip(payload[0],values))
        try:identity=a.archive_url_identity(m.clean(row['original']))
        except ValueError:continue
        if identity!=base:continue
        if str(row['statuscode'])!='200' or not re.fullmatch(r'\d{14}',row['timestamp']):continue
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


def observed_candidates(store,limit=10,site='both'):
    """Prioritize pages with missing bodies, then other fetched mirror pages."""
    m.initialize(store)
    store.db.execute('CREATE TABLE IF NOT EXISTS mirror_history(site TEXT,url TEXT,checked_at TEXT,status TEXT,data TEXT,error TEXT,PRIMARY KEY(site,url))');store.db.commit()
    sources={key:source for key,source in m.definitions().items() if source['adapter']=='mirror' and site in {'both',key}}
    candidates={}
    def add(key,value,priority):
        if key not in sources or not isinstance(value,str):return
        url=m.clean(value)
        if urlsplit(url).scheme not in {'http','https'} or not m.allowed(sources[key],url):return
        candidates[(key,url)]=min(candidates.get((key,url),2),priority)
    for row in store.db.execute("SELECT site,status,data FROM items"):
        record=json.loads(row['data']);add(row['site'],record.get('original_page_url') or record.get('page_url'),0 if row['status']=='failed' else 1)
    for row in store.db.execute('SELECT site,url,status FROM mirror_frontier WHERE path IS NOT NULL'):
        add(row['site'],row['url'],0 if row['status']=='failed' else 2)
    prior={(row['site'],row['url']):row for row in store.db.execute('SELECT site,url,status,checked_at FROM mirror_history')}
    keys=[key for key in candidates if key not in prior or prior[key]['status']!='ok']
    keys.sort(key=lambda key:(key in prior,prior[key]['checked_at'] if key in prior else '',candidates[key],key))
    # Interleave sources so one large catalogue does not postpone every other
    # mirror. Sites least recently queried take the first slots next pass.
    groups={}
    for key in keys:groups.setdefault(key[0],[]).append(key)
    latest={key:max((row['checked_at'] for (saved_site,_),row in prior.items() if saved_site==key),default='') for key in groups}
    sites=sorted(groups,key=lambda key:(latest[key],key));result=[]
    while sites and len(result)<limit:
        remaining=[]
        for key in sites:
            result.append(groups[key].pop(0))
            if groups[key]:remaining.append(key)
            if len(result)>=limit:return result
        sites=remaining
    return result


def discover_observed(store,fetcher,limit=10,site='both'):
    candidates=observed_candidates(store,limit,site);queued=0
    for key,url in candidates:
        source={**m.definitions()[key],'homes':[],'seeds':[url]}
        queued+=discover(store,fetcher,source)
    return len(candidates),queued


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--site',required=True,choices=['both']+list(m.definitions()))
    parser.add_argument('--observed',action='store_true',help='search distinct dates of already observed product/catalogue pages')
    parser.add_argument('--limit',type=a.positive,default=10);parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    if args.site=='both' and not args.observed:parser.error('--site both requires --observed')
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/('mirror-history-watch-'+args.site+'.lock'),blocking=False):
            while True:
                command=[sys.executable,'-u',__file__,'--root',args.root,'--site',args.site,'--limit',str(args.limit)]
                if args.observed:command.append('--observed')
                result=subprocess.run(command)
                print('mirror history batch exit',result.returncode,flush=True);time.sleep(60)
    else:
        with exclusive_file(Path(args.root)/'.claims'/('mirror-history-'+args.site+'.lock'),blocking=False):
            store=a.Store(args.root)
            try:
                fetcher=a.Fetcher(delay=10,timeout=30,retries=2)
                result=discover_observed(store,fetcher,args.limit,args.site) if args.observed else discover(store,fetcher,m.definitions()[args.site])
                print('history coverage',result,flush=True)
            finally:store.db.close()
