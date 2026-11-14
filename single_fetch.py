"""Archive an observed public source on Actions without a host origin-body copy."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import re
import uuid

import archiver as a
from archive_lock import exclusive_file
from bulk import publicly_archived, reuse_verified
import runner_upload as jobs
from runner_file_upload import MAX_BYTES, validate_spec, public_source
import staged_archive as staged
from transfer_probe import client as make_client, save

PART_SIZE=8*1024**2


def _load(store,ident):
    path=staged.journal_path(store,ident)
    if not path.exists():return None
    if path.is_symlink():raise ValueError('unsafe single-fetch journal')
    state=json.loads(path.read_text())
    if state.get('item_id')!=ident or not isinstance(state.get('source'),dict):raise ValueError('single-fetch journal identity')
    if state['source'].get('id')!=ident:raise ValueError('single-fetch source identity')
    if not isinstance(state.get('nonce'),str) or not re.fullmatch('[a-f0-9]{32}',state['nonce']):raise ValueError('invalid journal nonce')
    if not isinstance(state.get('staging_key'),str) or not re.fullmatch('staging/[a-f0-9]{64}',state['staging_key']):raise ValueError('invalid journal staging key')
    if state.get('data_sha256')!=hashlib.sha256(state['source']['data'].encode()).hexdigest():raise ValueError('frozen source changed')
    if state.get('run_id') is not None and (type(state['run_id']) is not int or state['run_id']<=0):raise ValueError('invalid workflow handle')
    if state.get('phase') not in {'preparing','job','parts','staged','verified','accepted','cleaned','aborted'}:raise ValueError('unknown single-fetch phase')
    if state['phase'] in {'job','parts','staged','verified','accepted','cleaned'}:
        spec=state.get('spec')
        if not isinstance(spec,dict):raise ValueError('missing frozen runner specification')
        validate_spec(spec)
        record=json.loads(state['source']['data'])
        expected=dict(mode='staging',staging_key=state['staging_key'],size=state['size'],part_size=state['part_size'],
            source_url=state['source_url'],sha256=record.get('expected_sha256'),md5=record.get('expected_md5'),published_size=record.get('expected_size'))
        if any(spec.get(key)!=value for key,value in expected.items()):raise ValueError('runner specification identity changed')
        from urllib.parse import parse_qs,urlsplit
        if any(parse_qs(urlsplit(part['url']).query).get('uploadId')!=[state.get('upload_id')] for part in spec['parts']):
            raise ValueError('runner multipart identity changed')
    return state


def _save(store,state):
    staged._save(store,state)


def _retire(store,state):
    old=staged.journal_path(store,state['item_id']).parent/'history'/(state['nonce']+'.json')
    save(old,state)


def _equivalents(store,source):
    record=json.loads(source['data']);md5=record.get('expected_md5');sha=record.get('expected_sha256')
    if sha:
        rows=store.db.execute("SELECT id FROM items WHERE json_extract(data,'$.expected_sha256')=? OR sha256=?",(sha,sha)).fetchall()
    elif md5 and record.get('brand'):
        rows=store.db.execute("""SELECT i.id FROM items i JOIN item_catalog c ON c.item_id=i.id JOIN products p ON p.id=c.product_id
            WHERE (json_extract(i.data,'$.expected_md5')=? OR i.md5=?) AND p.manufacturer_id=(
            SELECT p.manufacturer_id FROM item_catalog c JOIN products p ON p.id=c.product_id WHERE c.item_id=?)""",(md5,md5,source['id'])).fetchall()
    else:rows=[]
    return sorted({row[0] for row in rows}-{source['id']})


def _claim_aliases(stack,store,source):
    for ident in _equivalents(store,source):
        path=store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')
        stack.enter_context(exclusive_file(path,blocking=False))
        other=_load(store,ident)
        if other and (other['phase'] in {'parts','staged','verified','accepted'} or
                      other['phase']=='job' and other.get('dispatch_attempted_at')):
            raise BlockingIOError('equivalent source has a resumable single-fetch job')


def _abort(store,client,state):
    if state.get('run_id') and state.get('conclusion') is None:
        try:jobs.gh(['api','repos/'+jobs.REPO+'/actions/runs/'+str(state['run_id'])+'/cancel','--method','POST'])
        except RuntimeError:pass
    if state.get('upload_id'):
        try:client.abort_multipart_upload(Bucket=staged.BUCKET,Key=state['staging_key'],UploadId=state['upload_id'])
        except Exception as error:
            import multipart_upload as multipart
            if multipart._code(error)!='NoSuchUpload':raise
    state['phase']='aborted';state['aborted_at']=a.now();_save(store,state)


def _prepare(store,source,source_url,size,client):
    record=json.loads(source['data'])
    if source['path']:raise ValueError('existing local body requires the verified-local route')
    if (record.get('fields') or {}).get('source_association_quarantined'):raise ValueError('quarantined source')
    public_source(source_url)
    if source_url not in record.get('mirrors',[])+a.matching_mirrors(store,source,record):raise ValueError('source URL was not observed for this file')
    if size is None:size=record.get('expected_size')
    if type(size) is not int or not 0<size<=MAX_BYTES:raise ValueError('bounded exact transport size required')
    if record.get('expected_size') is not None and record['expected_size']!=size:raise ValueError('published size differs')
    head=jobs.gh(['api','repos/'+jobs.REPO+'/git/ref/heads/main'])['object']['sha']
    if not isinstance(head,str) or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}',head):raise ValueError('invalid workflow commit identity')
    state=dict(item_id=source['id'],source=dict(source),data_sha256=hashlib.sha256(source['data'].encode()).hexdigest(),
        nonce=uuid.uuid4().hex,staging_key='staging/'+uuid.uuid4().hex+uuid.uuid4().hex,
        size=size,part_size=PART_SIZE,source_url=source_url,head_sha=head,phase='preparing',created_at=a.now())
    _save(store,state)
    response=client.create_multipart_upload(Bucket=staged.BUCKET,Key=state['staging_key'])
    upload_id=response.get('UploadId')
    if not isinstance(upload_id,str) or not upload_id:raise ValueError('missing staging upload ID')
    state['upload_id']=upload_id;_save(store,state)
    parts=[]
    for number in range(1,(size+PART_SIZE-1)//PART_SIZE+1):
        count=min(PART_SIZE,size-(number-1)*PART_SIZE)
        url=client.generate_presigned_url('upload_part',Params={'Bucket':staged.BUCKET,'Key':state['staging_key'],
            'UploadId':upload_id,'PartNumber':number,'ContentLength':count},ExpiresIn=1800,HttpMethod='PUT')
        parts.append(dict(number=number,url=url))
    referer=a.original(record.get('page_url') or '')
    if referer:
        try:public_source(referer)
        except ValueError:referer=None
    spec=dict(mode='staging',staging_key=state['staging_key'],sha256=record.get('expected_sha256'),md5=record.get('expected_md5'),
        published_size=record.get('expected_size'),size=size,part_size=PART_SIZE,parts=parts,source_url=source_url,referer=referer)
    validate_spec(spec);state.update(spec=spec,phase='job');_save(store,state)
    return state


def run_claimed(store,ident,source_url=None,size=None,*,profile='hf',client=None,execute=None,readback=None,timeout=1500):
    """Caller holds the primary source claim; equivalent claims are nonblocking."""
    if type(timeout) is not int or not 0<timeout<=1500:raise ValueError('invalid observation window')
    state=_load(store,ident)
    current=store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()
    if state and state['phase'] in {'accepted','cleaned'}:
        client=client or make_client(profile)
        result=staged.promote(store,client,state['source'],state,state['receipt'],readback=readback)
        if state['phase']!='cleaned' or not current or hashlib.sha256(current['data'].encode()).hexdigest()==state['data_sha256']:return result
        _retire(store,state);state=None
    if state is None and current and publicly_archived(current,staged.NAMESPACE,staged.BUCKET):
        return dict(accepted_id=ident,reused=True,cleanup_pending=False)
    if state is None and current and reuse_verified(store,current,staged.NAMESPACE,staged.BUCKET):
        return dict(accepted_id=ident,reused=True,cleanup_pending=False)
    if state and state['phase'] in {'aborted','preparing'}:
        client=client or make_client(profile)
        if state['phase']=='preparing':_abort(store,client,state)
        if source_url is None:source_url=state['source_url']
        if size is None:size=state['size']
        _retire(store,state);state=None
    if not state and current is None:raise LookupError('source record missing')
    source=state['source'] if state else dict(current)
    with ExitStack() as claims:
        _claim_aliases(claims,store,source)
        client=client or make_client(profile)
        if state is None:state=_prepare(store,source,source_url,size,client)
        if state['phase']=='job':
            if not state.get('dispatch_attempted_at') and (current is None or (json.loads(current['data']).get('fields') or {}).get('source_association_quarantined')):
                _abort(store,client,state);raise ValueError('source was removed or quarantined before dispatch')
            try:
                receipt=(execute or jobs.execute_job)(state,state['spec'],staged.journal_path(store,ident),timeout=timeout)
            except jobs.RunnerIdentityMismatch:
                _abort(store,client,state);raise
            except TimeoutError:
                # Preserve the same run and capability identity on observation expiry.
                raise
            except Exception:
                if state.get('conclusion') not in (None,'success'):_abort(store,client,state)
                raise
            if state.get('conclusion')!='success' or type(state.get('run_id')) is not int or state['run_id']<=0:raise ValueError('terminal verified runner required')
            state.update(receipt=receipt,phase='parts',fetched_at=a.now());_save(store,state)
        return staged.promote(store,client,source,state,state['receipt'],readback=readback)


def run(store,ident,source_url=None,size=None,**kwargs):
    path=store.root/'.claims'/(hashlib.sha256(ident.encode()).hexdigest()+'.lock')
    with exclusive_file(path,blocking=False):return run_claimed(store,ident,source_url,size,**kwargs)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive');parser.add_argument('--id',required=True)
    parser.add_argument('--source-url');parser.add_argument('--size',type=a.positive)
    parser.add_argument('--profile',default='hf');args=parser.parse_args(argv)
    store=a.Store(args.root)
    try:
        result=run(store,args.id,args.source_url,args.size,profile=args.profile)
        print(json.dumps({key:result.get(key) for key in ['accepted_id','source_changed','cleanup_pending','reused']}),flush=True)
        return 0
    except Exception as error:
        print('single-fetch deferred',type(error).__name__,flush=True);return 1
    finally:store.db.close()


if __name__=='__main__':raise SystemExit(main())
