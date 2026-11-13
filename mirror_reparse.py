"""Apply current dedicated mirror parsers to saved HTML without network requests."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
import mirror_sources as m
import portals
from archive_lock import exclusive_file

PARSERS={key:'parse_'+key for key in ['multi_inside','mimimaid','nekoneko','tsundere','moepedia','dlsite','d_station','lapistan','kokoron','dmm','alice_archives','alicefree','getchu','xgame','redistribution','magic_mirror','fuzzy2','suezou']}


def reparse(store,site='both'):
    m.initialize(store)
    version=hashlib.sha256(Path(portals.__file__).read_bytes()+Path(m.__file__).read_bytes()+Path(a.__file__).read_bytes()).hexdigest()
    store.db.execute('CREATE TABLE IF NOT EXISTS mirror_reparse(site TEXT,url TEXT,parser_version TEXT,source_hash TEXT,parsed_at TEXT,files INTEGER,error TEXT,PRIMARY KEY(site,url))');store.db.commit()
    pages=store.db.execute("SELECT site,url,path FROM pages WHERE status='ok' AND (?='both' OR site=?)",(site,site)).fetchall()
    processed=found=failed=0
    for row in pages:
        if row['site'] not in PARSERS:continue
        path=store.root/row['path']
        try:
            raw=path.read_bytes();source_hash=hashlib.sha256(raw).hexdigest()
            saved=store.db.execute('SELECT * FROM mirror_reparse WHERE site=? AND url=?',(row['site'],row['url'])).fetchone()
            if saved and saved['parser_version']==version and saved['source_hash']==source_hash and not saved['error']:continue
            records=getattr(portals,PARSERS[row['site']])(a.decode_html(raw,None),row['url'])
            for record in records:
                m.add_record(store,record)
                for url in record['mirrors']:
                    store.db.execute('UPDATE mirror_links SET matched_item=? WHERE site=? AND parent_url=? AND url=?',(record['id'],row['site'],row['url'],m.clean(url)))
            store.db.execute('INSERT OR REPLACE INTO mirror_reparse VALUES(?,?,?,?,?,?,?)',(row['site'],row['url'],version,source_hash,a.now(),len(records),None));store.db.commit()
            processed+=1;found+=len(records)
            if records:print('reparsed',row['site'],len(records),row['url'],flush=True)
        except (OSError,ValueError) as error:
            store.db.rollback();failed+=1
            store.db.execute('INSERT OR REPLACE INTO mirror_reparse VALUES(?,?,?,?,?,?,?)',(row['site'],row['url'],version,None,a.now(),0,str(error)));store.db.commit();print('reparse failed',row['url'],error,flush=True)
    if processed:store.export()
    return processed,found,failed


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--site',default='both');parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    if args.watch:
        # A fresh subprocess sees parser updates without interrupting transfers
        # or holding old module code for the lifetime of a large source crawl.
        while True:
            subprocess.run([sys.executable,__file__,'--root',args.root,'--site',args.site],check=True)
            time.sleep(60)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'reparse.lock'):
            store=a.Store(args.root)
            try:print('reparse result',reparse(store,args.site),flush=True)
            finally:store.db.close()
