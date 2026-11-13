"""Archive public Drive mirrors explicitly linked by a creator-supplied note."""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit
import archiver as a
import bulk
import hanny_mods
import mirror_sources as m
from archive_lock import exclusive_file


def relative_path(value):
    path=PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value:
        raise ValueError('unsafe creator mirror path')
    return path.as_posix()


def discover(store,key):
    folder=store.root/'mega-mods'/key
    receipt_path=folder/'.native-complete.json'
    receipt_raw=receipt_path.read_bytes();receipt=json.loads(receipt_raw)
    candidates={}
    for name,expected in receipt.get('files',{}).items():
        name=relative_path(name)
        if '容量制限' not in name and 'ミラー' not in name:continue
        path=folder/name
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('creator note escapes native staging')
        if a.digest(path)!=expected:raise ValueError('creator note differs from native file receipt')
        raw=path.read_bytes()
        if len(raw)>1024*1024:raise ValueError('creator mirror note exceeds bound')
        for encoding in ['utf-8-sig','cp932']:
            try:text=raw.decode(encoding);break
            except UnicodeError:continue
        else:raise ValueError('creator mirror note encoding unsupported')
        for url in re.findall(r'https?://[^\s<>"]+',text):
            parts=urlsplit(url)
            if (parts.scheme=='https' and parts.hostname=='drive.google.com' and not parts.username
                    and re.fullmatch(r'/drive/(?:u/\d+/)?folders/[A-Za-z0-9_-]+/?',parts.path)):
                candidates[url]={'source_note':path.relative_to(store.root).as_posix(),
                    'source_note_sha256':expected['sha256'],'native_receipt_sha256':hashlib.sha256(receipt_raw).hexdigest(),
                    'discovery_basis':'creator note linked this public Drive mirror; native folder remains independently incomplete'}
    if len(candidates)!=1:raise ValueError('missing or ambiguous creator-published Drive mirror')
    return next(iter(candidates.items()))


def list_files(url):
    import gdown
    files=gdown.download_folder(url=url,skip_download=True,quiet=True,use_cookies=False,timeout=30,retries=1)
    if not files:raise ValueError('empty creator mirror inventory')
    return [{'id':file.id,'path':file.path} for file in files]


def records(entry,url,inventory,evidence):
    result=[];paths=set()
    for item in inventory:
        ident=item.get('id');relative=relative_path(item.get('path') or '')
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',ident):
            raise ValueError('invalid public Drive file ID')
        if relative in paths:raise ValueError('ambiguous creator mirror path')
        paths.add(relative);filename=PurePosixPath(relative).name
        if not filename:raise ValueError('empty creator mirror filename')
        document=filename.lower().endswith(('.txt','.md','.pdf'))
        variant='DL版・廉価版' if 'DL版' in relative else ('通常版' if '通常版' in relative else None)
        file_id=hashlib.sha256(('hanny-drive\n'+entry['page']+'\n'+ident+'\n'+relative).encode()).hexdigest()
        result.append({'id':file_id,'site':'hanny','page_url':entry['page'],'original_page_url':entry['page'],
            'title':entry['title'],'brand':'アリスソフト',
            'release_name':('説明書 ' if document else 'パッチ MOD ')+entry['release']+' '+relative,
            'filename':filename,'mirrors':['https://drive.google.com/file/d/'+ident+'/view'],
            'expected_size':None,'expected_md5':None,'published_at':None,'size_text':None,
            'fields':{'creator':entry['creator'],'source_folder':url,'drive_relative_path':relative,
                'creator_mirror_evidence':evidence,'source_integrity':'public HTTPS Drive download; measured hashes and full public read-back',
                '適用条件':variant,'source_kind':'creator-published Drive mirror'}})
    if not result:raise ValueError('empty creator mirror inventory')
    return result


def initialize(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS creator_drive_folders(
        source_key TEXT,folder_url TEXT,inventory_json TEXT,evidence_json TEXT,listed_at TEXT,
        status TEXT,verified_files INTEGER,PRIMARY KEY(source_key,folder_url))""");store.db.commit()


def update_state(store,key,url,items):
    verified=0
    for item in items:
        row=store.db.execute('SELECT * FROM items WHERE id=?',(item['id'],)).fetchone()
        if row and row['status'] in {'verified','archived'} and bulk.publicly_archived(row,'vn-archive','mirrors'):
            verified+=1
    status='complete' if verified==len(items) else 'partial'
    store.db.execute('UPDATE creator_drive_folders SET status=?,verified_files=? WHERE source_key=? AND folder_url=?',
        (status,verified,key,url));store.db.commit()
    return status,verified


def collect(store,entry,url,inventory,evidence,limit=None,list_only=False):
    items=records(entry,url,inventory,evidence);initialize(store)
    for item in items:m.add_record(store,item)
    store.db.execute('INSERT OR REPLACE INTO creator_drive_folders VALUES(?,?,?,?,?,?,?)',
        (entry['key'],url,json.dumps(inventory,ensure_ascii=False),json.dumps(evidence,ensure_ascii=False),a.now(),'partial',0));store.db.commit()
    processed=0
    try:
        if not list_only:
            for item in sorted(items,key=lambda r:(0 if r['filename'].lower().endswith('.zip') else 1,r['id'])):
                if limit is not None and processed>=limit:break
                with ExitStack() as claims:
                    try:
                        claims.enter_context(exclusive_file(store.root/'.claims'/(hashlib.sha256(item['id'].encode()).hexdigest()+'.lock'),blocking=False))
                    except BlockingIOError:continue
                    row=store.db.execute('SELECT * FROM items WHERE id=?',(item['id'],)).fetchone()
                    if row['status'] in {'verified','archived'} and bulk.publicly_archived(row,'vn-archive','mirrors'):continue
                    processed+=1
                    if a.download(store,a.Fetcher(delay=3,timeout=60,retries=2),site='hanny',ident=item['id'],export=False):continue
                    a.publish(store,'vn-archive','mirrors','hf',site='hanny',ident=item['id'],metadata=False)
                    bulk.evict_verified(store,item['id'],'vn-archive','mirrors')
                    update_state(store,entry['key'],url,items)
    finally:status,verified=update_state(store,entry['key'],url,items)
    return {'files':len(items),'processed':processed,'public_verified':verified,'status':status}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive')
    parser.add_argument('--source',required=True,choices=list(hanny_mods.definitions()))
    parser.add_argument('--limit',type=a.positive);parser.add_argument('--list-only',action='store_true');args=parser.parse_args()
    with exclusive_file(Path(args.root)/'.claims'/('creator-drive-'+args.source+'.lock'),blocking=False):
        store=a.Store(args.root)
        try:
            url,evidence=discover(store,args.source)
            print('creator Drive mirror',collect(store,hanny_mods.definitions()[args.source],url,list_files(url),evidence,args.limit,args.list_only),flush=True)
        finally:store.db.close()
