"""Run one bounded Actions transfer comparison; never complete its multipart upload."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import uuid

from archive_lock import exclusive_file
from runner_transfer_probe import MAX_BYTES,validate_spec

REPO='a-kaibu/mirror'
WORKFLOW='transfer-probe.yml'
PART_SIZE=8*1024**2


def now():return datetime.now(timezone.utc).isoformat()


def save(path,state):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    with open(temporary,'w',opener=lambda name,flags:os.open(name,flags,0o600)) as out:
        json.dump(state,out,indent=2);out.write('\n')
    os.chmod(temporary,0o600);temporary.replace(path)


def gh(arguments):
    result=subprocess.run(['gh',*arguments],capture_output=True,text=True)
    if result.returncode:raise RuntimeError('GitHub request failed')
    return json.loads(result.stdout) if result.stdout.strip() else None


def client():
    import boto3
    from botocore.config import Config
    # Explicit SigV4 also governs presigning; the SDK may otherwise choose V2.
    return boto3.Session(profile_name='hf').client('s3',endpoint_url='https://vn-archive.s3.hf.co',
        config=Config(signature_version='s3v4',region_name='us-east-1',s3={'addressing_style':'path'},
            request_checksum_calculation='when_required',response_checksum_validation='when_required',
            connect_timeout=10,read_timeout=60,retries={'total_max_attempts':2}))


def verify_receipt(state,receipt,server_parts):
    if (receipt.get('nonce')!=state['nonce'] or receipt.get('sha256')!=state['sha256']
            or receipt.get('size')!=state['size'] or receipt.get('completed_public_object') is not False):
        raise ValueError('receipt identity mismatch')
    parts=receipt.get('parts');count=(state['size']+PART_SIZE-1)//PART_SIZE
    if not isinstance(parts,list) or len(parts)!=count or len(server_parts)!=count:raise ValueError('receipt part count')
    for number,part in enumerate(parts,1):
        expected=min(PART_SIZE,state['size']-(number-1)*PART_SIZE);remote=server_parts[number-1]
        if (part.get('number')!=number or part.get('size')!=expected or remote.get('PartNumber')!=number
                or remote.get('Size')!=expected or not part.get('etag') or remote.get('ETag')!=part['etag']):
            raise ValueError('server acknowledgement mismatch')
    if not isinstance(receipt.get('upload_seconds'),(float,int)) or receipt['upload_seconds']<=0:raise ValueError('invalid timing')
    return receipt


def select_object(root,ident):
    db=sqlite3.connect('file:'+str((root/'index.sqlite3').absolute())+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        query="""SELECT id,sha256,size FROM items WHERE remote_sha256=sha256 AND remote_verified_at IS NOT NULL
            AND remote_key='vn-archive/mirrors/files/'||sha256 AND size BETWEEN ? AND ?
            AND coalesce(json_extract(data,'$.fields.source_association_quarantined'),0)=0"""
        params=[PART_SIZE,MAX_BYTES]
        if ident:query+=' AND id=?';params.append(ident)
        row=db.execute(query+' ORDER BY size,id LIMIT 1',params).fetchone()
        if not row:raise ValueError('no qualifying verified public object')
        return dict(row)
    finally:db.close()


def run(root,ident=None,resume=False,cleanup_only=False,timeout=1200):
    root=Path(root);journal=root/'.work'/'transfer-probe.json';s3=client()
    with exclusive_file(root/'.claims'/'transfer-probe.lock',blocking=False):
        state={}
        if journal.exists():
            previous=json.loads(journal.read_text())
            if not previous.get('cleanup_complete'):
                if not resume and not cleanup_only:raise ValueError('unfinished probe; use --resume or --cleanup-only')
                state=previous
        if cleanup_only and not state:return None
        if not state:
            state=select_object(root,ident);state.update(nonce=uuid.uuid4().hex,created_at=now(),repo=REPO,cleanup_complete=False)
            state['key']='files/'+state['sha256']
            head=s3.head_object(Bucket='mirrors',Key=state['key'])
            if head.get('ContentLength')!=state['size']:raise ValueError('public object size changed')
            state['original_etag']=head.get('ETag')
            state['head_sha']=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
            remote=gh(['api','repos/'+REPO+'/git/ref/heads/main'])
            if remote['object']['sha']!=state['head_sha']:raise ValueError('main does not match this checkout')
            state['upload_id']=s3.create_multipart_upload(Bucket='mirrors',Key=state['key'])['UploadId']
        result=None
        try:
            save(journal,state)
            if not cleanup_only:
                if not state.get('dispatch_attempted_at'):
                    count=(state['size']+PART_SIZE-1)//PART_SIZE;urls=[]
                    for number in range(1,count+1):
                        expected=min(PART_SIZE,state['size']-(number-1)*PART_SIZE)
                        urls.append(s3.generate_presigned_url('upload_part',Params={'Bucket':'mirrors','Key':state['key'],
                            'UploadId':state['upload_id'],'PartNumber':number,'ContentLength':expected},ExpiresIn=1800,HttpMethod='PUT'))
                    spec={'sha256':state['sha256'],'size':state['size'],'part_size':PART_SIZE,'part_urls':urls,
                          'source_url':'https://huggingface.co/buckets/vn-archive/mirrors/resolve/'+state['key']}
                    validate_spec(spec)
                    with tempfile.TemporaryDirectory(prefix='mirror-probe-dispatch-') as folder:
                        request=Path(folder)/'request.json'
                        save(request,{'ref':'main','inputs':{'nonce':state['nonce'],'spec':json.dumps(spec)}})
                        state['dispatch_attempted_at']=now();save(journal,state)
                        try:gh(['api','repos/'+REPO+'/actions/workflows/'+WORKFLOW+'/dispatches','--method','POST','--input',str(request)])
                        except RuntimeError:pass  # An acknowledgement can be lost; find this same nonce, never redispatch.
                deadline=time.monotonic()+timeout;last=None
                while time.monotonic()<deadline:
                    if not state.get('run_id'):
                        runs=gh(['api','repos/'+REPO+'/actions/workflows/'+WORKFLOW+'/runs?per_page=30'])['workflow_runs']
                        matches=[r for r in runs if r.get('display_title')=='Transfer probe '+state['nonce'] and r['head_sha']==state['head_sha']]
                        if matches:state['run_id']=matches[0]['id'];save(journal,state)
                    if state.get('run_id'):
                        run=gh(['api','repos/'+REPO+'/actions/runs/'+str(state['run_id'])])
                        if run['status']!=last:print('probe run',state['run_id'],run['status'],flush=True);last=run['status']
                        if run['status']=='completed':
                            state['conclusion']=run['conclusion'];save(journal,state)
                            if run['conclusion']!='success':raise RuntimeError('probe workflow failed')
                            break
                    time.sleep(15)
                else:raise TimeoutError('finite probe deadline')
                with tempfile.TemporaryDirectory(prefix='mirror-probe-receipt-') as folder:
                    gh(['run','download',str(state['run_id']),'--repo',REPO,'--name','transfer-probe','--dir',folder])
                    receipt=json.loads((Path(folder)/'transfer-probe.json').read_text())
                remote=s3.list_parts(Bucket='mirrors',Key=state['key'],UploadId=state['upload_id'])
                if remote.get('IsTruncated'):raise ValueError('unexpected extra parts')
                result=verify_receipt(state,receipt,remote.get('Parts',[]));state['result']=result
                print('server verified accepted bytes',state['size'],'upload seconds',result['upload_seconds'],flush=True)
        finally:
            if state.get('run_id') and state.get('conclusion') is None:
                try:gh(['api','repos/'+REPO+'/actions/runs/'+str(state['run_id'])+'/cancel','--method','POST'])
                except RuntimeError:pass
            try:
                s3.abort_multipart_upload(Bucket='mirrors',Key=state['key'],UploadId=state['upload_id'])
                state['cleanup_complete']=True
                after=s3.head_object(Bucket='mirrors',Key=state['key'])
                state['public_object_headers_unchanged']=(after.get('ContentLength')==state['size'] and after.get('ETag')==state.get('original_etag'))
            except Exception as error:
                if getattr(error,'response',{}).get('Error',{}).get('Code')=='NoSuchUpload':state['cleanup_complete']=True
                else:state['cleanup_error_type']=type(error).__name__
            state['checked_at']=now();save(journal,state)
            print('probe temporary upload aborted',state['cleanup_complete'],flush=True)
        if not state['cleanup_complete']:raise RuntimeError('probe cleanup requires retry')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default='archive');parser.add_argument('--id')
    parser.add_argument('--resume',action='store_true');parser.add_argument('--cleanup-only',action='store_true')
    args=parser.parse_args()
    try:run(args.root,args.id,args.resume,args.cleanup_only);return 0
    except Exception as error:
        print('transfer diagnostic failed',type(error).__name__)
        return 1


if __name__=='__main__':raise SystemExit(main())
