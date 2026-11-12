"""Collect creator-published Rance distributions from curated article bodies."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib
import unicodedata
from urllib.parse import urlsplit,unquote
import archiver as a
from drive_download import file_id
import mirror_sources as m


def entries():return tomllib.loads(Path(__file__).with_name('hanny_sources.toml').read_text())['entries']


def links(html,page,entry):
    if urlsplit(page).hostname!='hannylaboratory.blogspot.com' or page!=entry['page']:return []
    root=a.DOM(html).root;result={}
    for body in root.all({'div'}):
        if 'post-body' not in body.attrs.get('class','').split():continue
        for link in body.all({'a'}):
            target=a.resolve(page,link.attrs.get('href',''))
            if not target or not file_id(target):continue
            label=unicodedata.normalize('NFKC',link.text())
            if entry.get('label') and entry['label'] not in label:continue
            result[file_id(target)]=(target,label)
    return list(result.values())


def filename(fetcher,url):
    with fetcher.open(url) as response:raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024:raise ValueError('Drive metadata exceeds bound')
    root=a.DOM(a.decode_html(raw)).root
    names=[n.attrs.get('content','').strip() for n in root.all({'meta'}) if n.attrs.get('property')=='og:title']
    if len(names)!=1 or not a.EXT.search(names[0]):raise ValueError('Drive filename unavailable; requires review')
    return names[0]


def collect(store,fetcher):
    m.initialize(store)
    store.db.execute('CREATE TABLE IF NOT EXISTS hanny_scans(page TEXT,title TEXT,status TEXT,files INTEGER,checked_at TEXT,error TEXT,PRIMARY KEY(page,title))');store.db.commit()
    cache={};total=0
    for entry in entries():
        page=entry['page'];count=0
        try:
            if page not in cache:
                with fetcher.open(page) as response:raw=response.read(4*1024*1024+1);actual=response.geturl()
                if urlsplit(actual).hostname!='hannylaboratory.blogspot.com':raise ValueError('article left creator blog')
                if len(raw)>4*1024*1024:raise ValueError('article exceeds bound')
                rel='html/'+hashlib.sha256(page.encode()).hexdigest()+'.html';(store.root/rel).parent.mkdir(parents=True,exist_ok=True);(store.root/rel).write_bytes(raw)
                store.db.execute('INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,response_url) VALUES(?,?,?,?,?,?)',(page,'hanny',rel,a.now(),'ok',actual));store.db.commit();cache[page]=a.decode_html(raw)
            for target,label in links(cache[page],page,entry):
                name=filename(fetcher,target);ident=hashlib.sha256(('hanny\n'+page+'\n'+file_id(target)).encode()).hexdigest()
                release=('本編' if entry['kind']=='full' else 'パッチ')+' ver'+entry['version']
                fields={'distribution_kind':entry['kind'],'version':entry['version'],'creator':'TADA','download_label':label,'google_drive_id':file_id(target),'source_conditions':'再配布禁止の記載あり。配布元記事を参照。'}
                if entry['title']=='ランスクエスト':fields['適用条件']='DMM統合版 ver3.0' if 'DMM' in unicodedata.normalize('NFKC',name) else 'ランスクエスト＋マグナム ver2.082'
                record={'id':ident,'site':'hanny','page_url':page,'original_page_url':page,'title':entry['title'],'brand':'アリスソフト','release_name':release,'filename':name,'mirrors':[target],'expected_md5':None,'expected_size':None,'size_text':None,'published_at':None,'fields':fields}
                m.add_record(store,record);count+=1;total+=1;print('hanny registered',entry['title'],name,flush=True)
            if not count:raise ValueError('no supported published download links; requires review')
            store.db.execute('INSERT OR REPLACE INTO hanny_scans VALUES(?,?,?,?,?,?)',(page,entry['title'],'captured',count,a.now(),None))
        except (OSError,ValueError) as error:
            store.db.execute('INSERT OR REPLACE INTO hanny_scans VALUES(?,?,?,?,?,?)',(page,entry['title'],'review',count,a.now(),str(error)));print('hanny review',entry['title'],error,flush=True)
        store.db.commit()
    store.export();return total


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');args=parser.parse_args();store=a.Store(args.root)
    try:print('hanny files',collect(store,a.Fetcher(delay=3,timeout=30,retries=2)))
    finally:store.db.close()
