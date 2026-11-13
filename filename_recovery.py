"""Restore missing names only from same-maker, SHA-256-bound observations."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
import catalog
from archive_lock import exclusive_file


def public_bytes(row):
    return bool(row['sha256'] and row['remote_sha256']==row['sha256'] and row['remote_verified_at'])


def safe_name(value):
    return isinstance(value,str) and bool(value.strip()) and not any(c in '/\\' or ord(c)<32 or ord(c)==127 for c in value)


def candidates(store,row,record):
    rows=store.db.execute("""SELECT i.* FROM items i
      JOIN item_catalog c ON c.item_id=i.id JOIN products p ON p.id=c.product_id
      WHERE i.id!=? AND p.manufacturer_id=(SELECT p2.manufacturer_id
        FROM item_catalog c2 JOIN products p2 ON p2.id=c2.product_id WHERE c2.item_id=?)
      AND (i.sha256=? OR json_extract(i.data,'$.expected_sha256')=?)""",
      (row['id'],row['id'],row['sha256'],row['sha256'])).fetchall()
    result=[]
    for donor in rows:
        data=json.loads(donor['data']);name=data.get('filename')
        if not data.get('brand') or not safe_name(name):continue
        if data.get('expected_md5') and data['expected_md5']!=row['md5']:continue
        if data.get('expected_sha256') and data['expected_sha256']!=row['sha256']:continue
        if data.get('expected_size') is not None and data['expected_size']!=row['size']:continue
        published=data.get('expected_sha256')==row['sha256']
        verified=donor['sha256']==row['sha256'] and donor['status'] in {'verified','archived'} and donor['size']==row['size']
        if not (published or verified):continue
        result.append({'item_id':donor['id'],'page_url':data['page_url'],'filename':name,
            'sha256':row['sha256'],'evidence':'publisher-sha256' if published else 'verified-sha256'})
    return sorted(result,key=lambda p:(p['evidence']!='publisher-sha256',p['item_id']))


def recover(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS filename_recovery_receipts(
      item_id TEXT PRIMARY KEY,filename TEXT NOT NULL,sha256 TEXT NOT NULL,proof TEXT NOT NULL,recovered_at TEXT NOT NULL)""");store.db.commit()
    stats={'recovered':0,'unresolved':0,'deferred':0}
    rows=store.db.execute("""SELECT * FROM items WHERE sha256=remote_sha256 AND remote_verified_at IS NOT NULL
      AND (json_extract(data,'$.filename') IS NULL OR json_extract(data,'$.filename')='')""").fetchall()
    for saved in rows:
        try:
            claim=store.root/'.claims'/(hashlib.sha256(saved['id'].encode()).hexdigest()+'.lock')
            with exclusive_file(claim,blocking=False):
                row=store.db.execute('SELECT * FROM items WHERE id=?',(saved['id'],)).fetchone()
                if not row or not public_bytes(row):continue
                record=json.loads(row['data'])
                if record.get('filename'):continue
                if not record.get('brand'):stats['unresolved']+=1;continue
                proofs=candidates(store,row,record)
                if len({p['filename'] for p in proofs})!=1:stats['unresolved']+=1;continue
                proof=proofs[0];record['filename']=proof['filename']
                record['fields']={**(record.get('fields') or {}),'filename_recovered_from':proof}
                # Keep content proof and the catalog update atomic. A concurrent
                # source edit must not be overwritten by this descriptive change.
                with store.db:
                    changed=store.db.execute("""UPDATE items SET data=? WHERE id=? AND data=?
                      AND sha256=? AND remote_sha256=sha256 AND remote_verified_at IS NOT NULL""",
                      (json.dumps(record,ensure_ascii=False),row['id'],row['data'],row['sha256'])).rowcount
                    if not changed:stats['deferred']+=1;continue
                    catalog.index_record(store,record)
                    store.db.execute('INSERT OR REPLACE INTO filename_recovery_receipts VALUES(?,?,?,?,?)',
                        (row['id'],proof['filename'],row['sha256'],json.dumps(proofs,ensure_ascii=False),a.now()))
                stats['recovered']+=1
                print('recovered filename',row['id'],proof['filename'],proof['evidence'],flush=True)
        except BlockingIOError:stats['deferred']+=1
    if stats['recovered']:store.export()
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'filename-recovery-watch.lock'):
            while True:
                subprocess.run([sys.executable,'-u',__file__,'--root',args.root],check=True)
                time.sleep(300)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'filename-recovery.lock'):
            store=a.Store(args.root)
            try:print('filename recovery result',recover(store),flush=True)
            finally:store.db.close()
