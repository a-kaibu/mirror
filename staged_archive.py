"""Promote a completed single-fetch job; only public read-back accepts source proof.

The controller must hold its source claim and validate the terminal workflow/head
before calling this module. This module never dispatches or refetches an origin.
"""
import hashlib
import json
from pathlib import Path
import re

import archiver as a
from archive_lock import exclusive_file
import multipart_upload as multipart
from public_readback import digest_hashes
from runner_file_upload import MAX_BYTES, public_source
from transfer_probe import save

NAMESPACE='vn-archive'
BUCKET='mirrors'


def journal_path(store,ident):
    name=hashlib.sha256(ident.encode()).hexdigest()
    return store.root/'.work'/'single-fetch'/(name+'.json')


def _save(store,state):
    path=journal_path(store,state['item_id'])
    if path.is_symlink() or path.with_suffix('.tmp').is_symlink():raise ValueError('unsafe journal path')
    save(path,state)


def _head(client,key):
    try:return client.head_object(Bucket=BUCKET,Key=key)
    except Exception as error:
        if multipart._code(error) in {'404','NoSuchKey','NotFound'}:return None
        raise


def _receipt(source,state,result):
    if source.get('path'):raise ValueError('existing local body requires the verified-local route')
    record=json.loads(source['data'])
    if (record.get('fields') or {}).get('source_association_quarantined'):raise ValueError('quarantined source')
    if state.get('item_id')!=source['id'] or state.get('data_sha256')!=hashlib.sha256(source['data'].encode()).hexdigest():raise ValueError('source journal identity changed')
    if not isinstance(state.get('staging_key'),str) or not re.fullmatch('staging/[a-f0-9]{64}',state['staging_key']):raise ValueError('invalid staging key')
    size=state.get('size');part_size=state.get('part_size')
    if type(size) is not int or not 0<size<=MAX_BYTES:raise ValueError('invalid staging size')
    if type(part_size) is not int or not 5*1024**2<=part_size<=64*1024**2:raise ValueError('invalid staging part size')
    public_source(state.get('source_url'))
    if not state.get('nonce') or result.get('nonce')!=state['nonce'] or result.get('completed_public_object') is not False:raise ValueError('staging receipt identity')
    sha=result.get('sha256');md5=result.get('md5')
    if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('invalid receipt SHA-256')
    if not isinstance(md5,str) or not re.fullmatch('[a-f0-9]{32}',md5):raise ValueError('invalid receipt MD5')
    if type(result.get('size')) is not int or result['size']!=size:raise ValueError('receipt size mismatch')
    for field,actual in [('expected_sha256',sha),('expected_md5',md5),('expected_size',size)]:
        if record.get(field) is not None and record[field]!=actual:raise ValueError('published constraint mismatch')
    parts=result.get('parts');count=(size+part_size-1)//part_size
    if not isinstance(parts,list) or len(parts)!=count:raise ValueError('staging receipt part count')
    for number,part in enumerate(parts,1):
        expected=min(part_size,size-(number-1)*part_size)
        if (type(part.get('number')) is not int or part['number']!=number or type(part.get('size')) is not int or part['size']!=expected
                or not isinstance(part.get('etag'),str) or not part['etag']
                or not isinstance(part.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',part['sha256'])):raise ValueError('invalid staging part receipt')
    return dict(sha256=sha,md5=md5,size=size)


def _saved_proof(state,expected):
    proof=state.get('proof') or {}
    if {k:proof.get(k) for k in expected}!=expected or not isinstance(proof.get('verified_at'),str) or not proof['verified_at']:raise ValueError('saved public proof mismatch')
    return proof


def _cleanup(store,client,state):
    try:client.delete_object(Bucket=BUCKET,Key=state['staging_key'])
    except Exception as error:
        state['cleanup_error_type']=type(error).__name__;_save(store,state)
        return False
    state.pop('cleanup_error_type',None);state['phase']='cleaned';_save(store,state)
    return True


def promote(store,client,source,state,result,*,readback=None):
    """Resume staging/completion/copy/proof/CAS without an origin body request."""
    expected=_receipt(source,state,result)
    if store.db.in_transaction:raise ValueError('promotion requires committed metadata')
    if state.get('phase') not in {'parts','staged','verified','accepted','cleaned'}:raise ValueError('invalid promotion phase')
    if state['phase'] in {'accepted','cleaned'}:
        _saved_proof(state,expected)
        if state['phase']=='accepted':_cleanup(store,client,state)
        return dict(accepted_id=state.get('accepted_id'),proof=state['proof'],cleanup_pending=state['phase']!='cleaned')
    key='files/'+expected['sha256']
    claim=hashlib.sha256(('s3-upload\n'+NAMESPACE+'\n'+BUCKET+'\n'+key).encode()).hexdigest()
    if state['phase']=='parts':
        staged=_head(client,state['staging_key'])
        if staged is None:
            remote=multipart._parts(client,BUCKET,state['staging_key'],state['upload_id'])
            if set(remote)!=set(range(1,len(result['parts'])+1)):raise ValueError('server staging part count')
            for part in result['parts']:
                observed=remote[part['number']]
                if observed['Size']!=part['size'] or observed['ETag']!=part['etag']:raise ValueError('server staging part acknowledgement')
            client.complete_multipart_upload(Bucket=BUCKET,Key=state['staging_key'],UploadId=state['upload_id'],MultipartUpload={
                'Parts':[dict(PartNumber=p['number'],ETag=p['etag']) for p in result['parts']]})
            staged=client.head_object(Bucket=BUCKET,Key=state['staging_key'])
        if staged.get('ContentLength')!=expected['size'] or not isinstance(staged.get('ETag'),str) or not staged['ETag']:raise ValueError('staged object headers')
        state.update(phase='staged',staging_etag=staged['ETag']);_save(store,state)
    with exclusive_file(store.root/'.claims'/('upload-'+claim+'.lock'),blocking=False):
        if state['phase']=='staged':
            existing=_head(client,key)
            if existing is None:
                client.copy_object(Bucket=BUCKET,Key=key,CopySource={'Bucket':BUCKET,'Key':state['staging_key']},CopySourceIfMatch=state['staging_etag'])
                existing=client.head_object(Bucket=BUCKET,Key=key)
            if existing.get('ContentLength')!=expected['size']:raise ValueError('canonical public size mismatch')
            if readback is None:
                fetcher=a.Fetcher(delay=0,timeout=60)
                proof=digest_hashes(a.public_url(NAMESPACE,BUCKET,key),fetcher.open)
            else:proof=readback(key)
            if proof!=expected:raise ValueError('canonical public hash or size mismatch')
            state.update(phase='verified',proof=dict(proof,verified_at=a.now()));_save(store,state)
        proof=_saved_proof(state,expected)
        columns=dict(status='archived',path=None,md5=proof['md5'],sha256=proof['sha256'],size=proof['size'],
            source_url=state['source_url'],fetched_at=state.get('fetched_at') or proof['verified_at'],error=None,
            remote_key=NAMESPACE+'/'+BUCKET+'/'+key,remote_sha256=proof['sha256'],remote_verified_at=proof['verified_at'])
        with store.db:
            changed=store.db.execute('UPDATE items SET '+','.join(k+'=?' for k in columns)+' WHERE id=? AND data=?',
                (*columns.values(),source['id'],source['data'])).rowcount
            accepted=source['id'] if changed else None
            if not changed:
                current=store.db.execute('SELECT data FROM items WHERE id=?',(source['id'],)).fetchone()
                if current:
                    saved=dict(source);saved.update(columns)
                    if (json.loads(current['data']).get('fields') or {}).get('source_association_quarantined'):
                        record=json.loads(saved['data']);record['fields']={**(record.get('fields') or {}),'source_association_quarantined':True}
                        saved['data']=json.dumps(record,ensure_ascii=False)
                    accepted=store.preserve_public_version(saved)
        state.update(phase='accepted',accepted_id=accepted,source_changed=not bool(changed));_save(store,state)
        _cleanup(store,client,state)
        return dict(accepted_id=accepted,proof=proof,source_changed=not bool(changed),cleanup_pending=state['phase']!='cleaned')
