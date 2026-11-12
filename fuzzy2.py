"""Collect early Fuzzy2 evaluation builds with their published checksum files."""
import argparse
import hashlib
import http.client
import re
import time
from pathlib import Path
from urllib.parse import urlsplit,unquote
import archiver as a
import mirror_sources as m
from archive_lock import exclusive_file


def parse(html,page):
    original=a.original(page);parts=urlsplit(original)
    if parts.hostname!='mirror.fuzzy2.com' or parts.path!='/typemoon/index.html':return []
    root=a.DOM(html).root;text=root.text()
    if not all(word in text for word in ['TYPE-MOON','幻視同盟','吉里吉里']):return []
    conditions='\n'.join(t.text() for t in root.all({'table'}) if '※注意事項※' in t.text())
    result=[]
    for table in root.all({'table'}):
        rows={}
        for tr in table.all({'tr'}):
            cells=[n for n in tr.children if isinstance(n,a.Node) and n.tag in {'td','th'}]
            if len(cells)>1:rows[cells[0].text().strip()]=cells[1:]
        required=['帯域','ファイルサイズ','他バージョンとの差異','実行ファイル','MD5ファイル']
        if not all(key in rows for key in required) or len({len(rows[key]) for key in required})!=1:continue
        for column in range(len(rows['帯域'])):
            targets=[]
            for key in ['実行ファイル','MD5ファイル']:
                links=[a.resolve(page,n.attrs.get('href','')) for n in rows[key][column].all({'a'})]
                links=[u for u in links if u]
                if len(links)!=1:break
                targets.append(links[0])
            if len(targets)!=2:continue
            target,checksum=targets;raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name)
            if not a.EXT.search(urlsplit(raw).path) or unquote(Path(urlsplit(a.original(checksum)).path).name)!=filename+'.md5':continue
            stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
            mirrors=([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw]
            if stamp:checksum=a.replay(a.original(checksum),stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)
            band=rows['帯域'][column].text();audio=rows['他バージョンとの差異'][column].text()
            ident=hashlib.sha256(('fuzzy2\n'+original+'\n'+filename).encode()).hexdigest()
            result.append({'id':ident,'site':'fuzzy2','page_url':page,'original_page_url':original,
                'title':'幻視同盟','brand':'TYPE-MOON','release_name':'評価版 '+band+' '+audio,'filename':filename,
                'expected_md5':None,'expected_size':None,'size_text':rows['ファイルサイズ'][column].text(),
                'published_at':None,'mirrors':mirrors,'fields':{'published_checksum_url':checksum,
                    '配布帯域':band,'音声仕様':audio,'適用条件':conditions,'distribution_kind':'trial'}})
    return result


def checksum(store,fetcher,url):
    cached=store.db.execute("SELECT path,response_url FROM pages WHERE url=? AND status='ok'",(url,)).fetchone()
    if cached:
        data=(store.root/cached['path']).read_bytes();actual=cached['response_url'] or url
    else:
        with a.open_archive_page(fetcher,url) as response:
            data=response.read(4097);actual=response.geturl()
    if len(data)>4096:raise ValueError('checksum document exceeds bound')
    if a.archive_url_identity(a.original(actual))!=a.archive_url_identity(a.original(url)):
        raise ValueError('checksum redirected to another source file')
    if a.WAYBACK.match(url) and not a.WAYBACK.match(actual):raise ValueError('checksum replay left archive')
    value=data.strip()
    if not re.fullmatch(b'[a-fA-F0-9]{32}',value):raise ValueError('checksum is not the published bare MD5 format')
    if not cached:
        rel='checksums/'+hashlib.sha256(url.encode()).hexdigest()+'.md5'
        path=store.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)",(url,'fuzzy2',rel,a.now(),'ok',None,actual));store.db.commit()
    return value.decode().lower(),actual,hashlib.sha256(data).hexdigest()


def collect(store,fetcher):
    store.db.execute('CREATE TABLE IF NOT EXISTS fuzzy2_checksums(item_id TEXT,checksum_url TEXT,checked_at TEXT,status TEXT,error TEXT,PRIMARY KEY(item_id,checksum_url))');store.db.commit()
    added=failed=0
    for page in store.db.execute("SELECT url,path FROM pages WHERE site='fuzzy2' AND status='ok'").fetchall():
        parts=urlsplit(a.original(page['url']))
        if parts.path!='/typemoon/index.html':continue
        for record in parse(a.decode_html((store.root/page['path']).read_bytes()),page['url']):
            url=record['fields']['published_checksum_url']
            try:
                value,actual,sha=checksum(store,fetcher,url)
                record['expected_md5']=value;record['fields'].update(checksum_source_url=actual,checksum_document_sha256=sha)
                m.add_record(store,record)
                store.db.execute('INSERT OR REPLACE INTO fuzzy2_checksums VALUES(?,?,?,?,?)',(record['id'],url,a.now(),'verified',None));store.db.commit()
                added+=1;print('fuzzy2 checksum verified',record['filename'],value,flush=True)
            except (OSError,ValueError,http.client.HTTPException) as error:
                store.db.execute('INSERT OR REPLACE INTO fuzzy2_checksums VALUES(?,?,?,?,?)',(record['id'],url,a.now(),'failed',str(error)));store.db.commit()
                failed+=1;print('fuzzy2 checksum failed',record['filename'],error,flush=True)
    return added,failed


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    with exclusive_file(Path(args.root)/'.claims'/'fuzzy2-checksums.lock'):
        store=a.Store(args.root)
        try:
            while True:
                print('fuzzy2 checksum collection',collect(store,a.Fetcher(delay=10,timeout=30,retries=2)),flush=True)
                if not args.watch:break
                time.sleep(300)
        finally:store.db.close()
