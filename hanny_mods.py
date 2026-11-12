"""Preserve creator-linked MEGA mod folders using a verified native client."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from urllib.parse import urljoin,urlsplit,unquote
import archiver as a
from archive_lock import exclusive_file
import bulk
import mirror_sources as m


def definitions():return {v['key']:v for v in tomllib.loads(Path(__file__).with_name('hanny_sources.toml').read_text())['mods']}


def source(store,fetcher,entry):
    page=entry['page']
    with fetcher.open(page) as response:raw=response.read(4*1024*1024+1);actual=response.geturl()
    if len(raw)>4*1024*1024 or urlsplit(actual).hostname!='hannylaboratory.blogspot.com':raise ValueError('invalid creator article')
    rel='html/'+hashlib.sha256(page.encode()).hexdigest()+'.html';(store.root/rel).parent.mkdir(parents=True,exist_ok=True);(store.root/rel).write_bytes(raw)
    store.db.execute('INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,response_url) VALUES(?,?,?,?,?,?)',(page,'hanny',rel,a.now(),'ok',actual));store.db.commit()
    targets=[]
    for body in a.DOM(a.decode_html(raw)).root.all({'div'}):
        if 'post-body' not in body.attrs.get('class','').split():continue
        for link in body.all({'a'}):
            target=urljoin(page,link.attrs.get('href',''))
            if urlsplit(target).hostname==entry['link_host']:targets.append(target)
    if len(set(targets))!=1:raise ValueError('missing or ambiguous creator download folder')
    target=targets[0]
    if urlsplit(target).hostname!='mega.nz':
        with fetcher.open(target) as response:target=response.geturl()
    target=unquote(target);parts=urlsplit(target)
    if parts.scheme!='https' or parts.hostname!='mega.nz' or not re.fullmatch(r'/folder/[A-Za-z0-9_-]+',parts.path) or not re.fullmatch(r'[A-Za-z0-9_-]+',parts.fragment):raise ValueError('not a complete published public MEGA folder')
    return target


def ingest(store,entry,folder,url,upload=True,receipt=None):
    folder=Path(folder).resolve();count=0
    if upload and (not receipt or receipt.get('url')!=url):raise ValueError('authenticated native download receipt required')
    for original in sorted(folder.rglob('*')):
        if original.is_symlink():raise ValueError('symlink in downloaded folder')
        if not original.is_file() or original.name=='.native-complete.json':continue
        if original.name.endswith(('.mega','.part','.tmp')):raise ValueError('incomplete native-client file')
        relative=original.relative_to(folder).as_posix();actual=a.digest(original)
        if not actual['size']:raise ValueError('empty downloaded mod file')
        if receipt and receipt['files'].get(relative)!=actual:raise ValueError('downloaded bytes differ from native completion receipt')
        ident=hashlib.sha256(('hanny-mega\n'+entry['page']+'\n'+relative+'\n'+actual['sha256']).encode()).hexdigest()
        variant='DL版・廉価版' if 'DL' in relative else ('パッケージ版' if 'パッケージ' in relative or '通常版' in relative else None)
        fields={'distribution_kind':'patch','creator':entry['creator'],'mega_relative_path':relative,'source_folder':url,'source_integrity':'megatools completed authenticated download' if receipt else 'local import only','適用条件':'製品版の戦国ランスとランス10が必要'+('。'+variant if variant else ''),'source_conditions':'配布元記事に再配布禁止・利用条件の記載あり。'}
        record={'id':ident,'site':'hanny','page_url':entry['page'],'original_page_url':entry['page'],'title':entry['title'],'brand':'アリスソフト','release_name':'パッチ MOD '+entry['release']+' '+relative,'filename':original.name,'mirrors':[url],'expected_md5':None,'expected_size':None,'published_at':None,'size_text':None,'fields':fields}
        with exclusive_file(store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')):
            m.add_record(store,record);row=store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()
            if bulk.publicly_archived(row,'vn-archive','mirrors'):original.unlink();continue
            name=re.sub(r'[^\w.() -]','_',original.name)[:150].strip('. ') or ident
            destination=store.root/'files'/'hanny'/ident/name;destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                if a.digest(destination)['sha256']!=actual['sha256']:raise ValueError('managed destination differs')
                original.unlink()
            else:original.replace(destination)
            result=a.validate(destination,record)
            if not store.update_if_current(ident,row['data'],status='verified',path=destination.relative_to(store.root).as_posix(),source_url=url,fetched_at=a.now(),error=None,remote_sha256=None,**result):raise ValueError('mod metadata changed during import')
            if upload:
                a.publish(store,'vn-archive','mirrors',ident=ident,metadata=False)
                bulk.evict_verified(store,ident,'vn-archive','mirrors')
            count+=1;print('mod imported',entry['key'],relative,result['size'],flush=True)
    if upload:
        for relative,expected in receipt['files'].items():
            ident=hashlib.sha256(('hanny-mega\n'+entry['page']+'\n'+relative+'\n'+expected['sha256']).encode()).hexdigest()
            with exclusive_file(store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')):
                row=store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()
                if not row or row['sha256']!=expected['sha256'] or row['size']!=expected['size']:raise ValueError('native receipt file missing from archive: '+relative)
                if not bulk.publicly_archived(row,'vn-archive','mirrors'):
                    a.publish(store,'vn-archive','mirrors',ident=ident,metadata=False)
                    bulk.evict_verified(store,ident,'vn-archive','mirrors')
        a.publish_metadata(store,'vn-archive','mirrors',include_html=False)
    store.export();return count


def write_receipt(path,receipt):
    temporary=path.with_suffix('.pending.json')
    temporary.write_text(json.dumps(receipt,ensure_ascii=False))
    temporary.replace(path)


def download_native(client,staging,url,receipt_path):
    import tempfile
    staging=Path(staging).resolve();receipt_path=Path(receipt_path)
    receipt=json.loads(receipt_path.read_text()) if receipt_path.exists() else {'url':url,'files':{}}
    if receipt['url']!=url:raise ValueError('public folder changed; preserve prior staging separately')
    # Completed files without a receipt are quarantined, so native verification
    # is performed rather than trusting an arbitrary existing filename.
    for path in list(staging.rglob('*')):
        if path.is_symlink():raise ValueError('symlink in native output')
        if not path.is_file() or path==receipt_path or path.name.startswith('.megatmp'):continue
        relative=path.relative_to(staging).as_posix()
        if receipt['files'].get(relative)==a.digest(path):continue
        held=staging.parent.parent/'mega-unverified'/staging.name/relative
        held.parent.mkdir(parents=True,exist_ok=True)
        if held.exists():held=held.with_name(held.name+'.'+a.digest(path)['sha256'])
        path.replace(held)
    receipt['complete']=False;write_receipt(receipt_path,receipt)
    args=[client,'--ignore-config-file','--no-ask-password','--no-progress','--print-names','--limit-speed','2048','--path',str(staging),url]
    with tempfile.TemporaryFile(mode='w+',encoding='utf-8') as errors:
        process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=errors,text=True,encoding='utf-8')
        try:
            for line in process.stdout:
                print(line.rstrip(),flush=True)
                path=Path(line.rstrip('\r\n'))
                if not path.is_absolute() or not path.is_file():continue
                relative=path.resolve().relative_to(staging).as_posix()
                if path.is_symlink() or path.name.startswith('.megatmp'):raise ValueError('invalid native completion path')
                receipt['files'][relative]=a.digest(path)
                write_receipt(receipt_path,receipt)
            code=process.wait()
        except BaseException:
            process.terminate();process.wait();raise
        finally:process.stdout.close()
        errors.seek(0);messages=errors.read();unexpected=[]
        for line in messages.splitlines():
            prefix='ERROR: File already exists at '
            if line.startswith(prefix):
                path=Path(line[len(prefix):])
                try:relative=path.resolve().relative_to(staging).as_posix()
                except ValueError:unexpected.append(line);continue
                if path.is_file() and receipt['files'].get(relative)==a.digest(path):continue
            unexpected.append(line)
        if unexpected or (code and not messages.strip()):
            print(messages,flush=True);raise subprocess.CalledProcessError(code or 1,args)
    inventory={}
    for path in staging.rglob('*'):
        if not path.is_file() or path==receipt_path:continue
        if path.is_symlink() or path.name.startswith('.megatmp') or path.name.endswith(('.mega','.part','.tmp')):raise ValueError('native download incomplete')
        relative=path.relative_to(staging).as_posix();actual=a.digest(path)
        if receipt['files'].get(relative)!=actual:raise ValueError('unverified native output')
        inventory[relative]=actual
    if not inventory:raise ValueError('empty native download')
    receipt.update(complete=True,completed_at=a.now(),files=inventory)
    write_receipt(receipt_path,receipt);return receipt


def collect(root,key,client,import_only=False):
    entry=definitions()[key];store=a.Store(root)
    try:
        m.initialize(store)
        staging=store.root/'mega-mods'/key;staging.mkdir(parents=True,exist_ok=True)
        receipt_path=staging/'.native-complete.json'
        if shutil.disk_usage(store.root).free<32*1024**3:raise OSError('less than 32 GiB working space')
        with exclusive_file(store.root/'.claims'/('mega-folder-'+key+'.lock')):
            if import_only:
                receipt=json.loads(receipt_path.read_text());url=receipt['url']
                if receipt.get('complete') is not True:raise ValueError('native folder download is not complete')
            else:
                url=source(store,a.Fetcher(delay=3,timeout=30,retries=2),entry)
                receipt=download_native(client,staging,url,receipt_path)
            print('mod folder archived',key,ingest(store,entry,staging,url,receipt=receipt),flush=True)
    finally:store.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--source',choices=list(definitions()),required=True);parser.add_argument('--megadl',default='megadl');parser.add_argument('--import-only',action='store_true');args=parser.parse_args();collect(args.root,args.source,args.megadl,args.import_only)
