"""Opt-in delegated part transport; completion and source proof stay on the host."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urldefrag

import multipart_upload as m
from runner_file_upload import MAX_BYTES,encode_spec,validate_spec
from transfer_probe import gh,save,now,REPO

WORKFLOW='runner-upload.yml'


class PartClient:
    delegated_uploads=True

    def __init__(self,store,base,namespace,bucket,profile,path,sha,size,source_url,referer=None,*,signer=None,timeout=1500):
        if (namespace,bucket)!=('vn-archive','mirrors'):raise ValueError('runner destination is not configured')
        if type(size) is not int or not 0<size<=MAX_BYTES:raise ValueError('runner file exceeds bounded size')
        self.root=store.root;self.base=base;self.namespace=namespace;self.bucket=bucket;self.profile=profile
        self.path=Path(path);self.sha=sha;self.size=size;self.source_url=source_url;self.referer=urldefrag(referer)[0] if isinstance(referer,str) else None
        self.signer=signer;self.timeout=timeout;self.responses={};self.key='files/'+sha

    def __getattr__(self,name):return getattr(self.base,name)

    def _cache(self):
        db=sqlite3.connect(m.cache_path(self.root),timeout=30);db.row_factory=sqlite3.Row
        return db

    def _manifest(self,upload_id):
        db=self._cache()
        try:
            state=db.execute('SELECT * FROM _s3_uploads WHERE namespace=? AND bucket=? AND key=?',
                             (self.namespace,self.bucket,self.key)).fetchone()
            if not state or (state['upload_id'],state['sha256'],state['size'])!=(upload_id,self.sha,self.size):
                raise ValueError('runner multipart identity changed')
            part_size=state['part_size']
            saved={r['number']:dict(r) for r in db.execute('SELECT * FROM _s3_upload_parts WHERE namespace=? AND bucket=? AND key=?',
                        (self.namespace,self.bucket,self.key))}
        finally:db.close()
        remote=m._parts(self.base,self.bucket,self.key,upload_id)
        count=(self.size+part_size-1)//part_size
        if any(n>count for n in remote):raise ValueError('unexpected server part')
        manifest={};missing=[];whole=hashlib.sha256()
        with self.path.open('rb') as source:
            for number in range(1,count+1):
                expected=min(part_size,self.size-(number-1)*part_size);left=expected;digest=hashlib.sha256()
                while left:
                    block=source.read(min(left,1024**2))
                    if not block:raise ValueError('runner local body shortened')
                    left-=len(block);digest.update(block);whole.update(block)
                sha=digest.hexdigest();manifest[number]={'size':expected,'sha256':sha}
                prior=saved.get(number);observed=remote.get(number)
                if not (prior and observed and prior['sha256']==sha and prior['size']==expected
                        and observed['Size']==expected and observed['ETag']==prior['etag']):missing.append(number)
            if source.read(1) or whole.hexdigest()!=self.sha:raise ValueError('runner local body changed')
        return part_size,manifest,missing

    def _signer(self):
        if self.signer is None:
            import boto3
            from botocore.config import Config
            self.signer=boto3.Session(profile_name=self.profile).client('s3',endpoint_url=self.base.meta.endpoint_url,
                config=self.base.meta.config.merge(Config(signature_version='s3v4')))
        return self.signer

    def _journal(self,upload_id):
        name=hashlib.sha256((self.namespace+'\n'+self.bucket+'\n'+self.key+'\n'+upload_id).encode()).hexdigest()
        return self.root/'.work'/'runner-uploads'/(name+'.json')

    def _mark_delegated(self,upload_id):
        db=self._cache()
        try:
            with db:
                changed=db.execute("UPDATE _s3_uploads SET phase='delegated' WHERE namespace=? AND bucket=? AND key=? AND upload_id=? AND sha256=?",
                    (self.namespace,self.bucket,self.key,upload_id,self.sha)).rowcount
                if changed!=1:raise ValueError('runner checkpoint changed')
        finally:db.close()

    def _execute(self,state,spec,journal):
        if not state.get('dispatch_attempted_at'):
            state['dispatch_attempted_at']=now();save(journal,state)
            with tempfile.TemporaryDirectory(prefix='mirror-runner-dispatch-') as folder:
                request=Path(folder)/'request.json';save(request,{'ref':'main','inputs':{'nonce':state['nonce'],'spec':encode_spec(spec)}})
                try:gh(['api','repos/'+REPO+'/actions/workflows/'+WORKFLOW+'/dispatches','--method','POST','--input',str(request)])
                except RuntimeError:pass  # Observe the same nonce after a lost acknowledgement.
        deadline=time.monotonic()+self.timeout;last=None
        while time.monotonic()<deadline:
            observed=None
            try:
                if not state.get('run_id'):
                    runs=gh(['api','repos/'+REPO+'/actions/workflows/'+WORKFLOW+'/runs?per_page=30'])['workflow_runs']
                    matches=[r for r in runs if r.get('display_title')=='Runner upload '+state['nonce']]
                    if matches:
                        state['run_id']=matches[0]['id'];save(journal,state)
                if state.get('run_id'):observed=gh(['api','repos/'+REPO+'/actions/runs/'+str(state['run_id'])])
            except RuntimeError:pass  # Poll failure is not job completion or permission to redispatch.
            if observed:
                if observed['head_sha']!=state['head_sha']:raise ValueError('runner commit identity changed')
                if observed['status']!=last:print('runner upload',state['run_id'],observed['status'],flush=True);last=observed['status']
                if observed['status']=='completed':
                    state['conclusion']=observed['conclusion'];save(journal,state)
                    if state['conclusion']!='success':raise RuntimeError('runner transfer failed')
                    break
            time.sleep(15)
        else:raise TimeoutError('runner transfer observation deadline')
        with tempfile.TemporaryDirectory(prefix='mirror-runner-receipt-') as folder:
            gh(['run','download',str(state['run_id']),'--repo',REPO,'--name','runner-upload','--dir',folder])
            return json.loads((Path(folder)/'runner-upload.json').read_text())

    def _verify_and_seed(self,state,receipt,manifest):
        if (receipt.get('nonce')!=state['nonce'] or receipt.get('sha256')!=self.sha or receipt.get('size')!=self.size
                or receipt.get('completed_public_object') is not False):raise ValueError('runner receipt identity mismatch')
        parts=receipt.get('parts')
        if not isinstance(parts,list) or [p.get('number') for p in parts]!=state['numbers']:raise ValueError('runner receipt part selection')
        remote=m._parts(self.base,self.bucket,self.key,state['upload_id']);accepted={}
        for part in parts:
            number=part['number'];expected=manifest[number];observed=remote.get(number)
            if (part.get('size')!=expected['size'] or part.get('sha256')!=expected['sha256'] or not part.get('etag')
                    or not observed or observed['Size']!=expected['size'] or observed['ETag']!=part['etag']):
                raise ValueError('runner part differs from local bytes or server acknowledgement')
            accepted[number]=part
        db=self._cache()
        try:
            with db:
                changed=db.execute("UPDATE _s3_uploads SET phase='active' WHERE namespace=? AND bucket=? AND key=? AND upload_id=? AND sha256=?",
                    (self.namespace,self.bucket,self.key,state['upload_id'],self.sha)).rowcount
                if changed!=1:raise ValueError('runner checkpoint changed')
                db.executemany('INSERT OR REPLACE INTO _s3_upload_parts VALUES(?,?,?,?,?,?,?)',
                    ((self.namespace,self.bucket,self.key,n,p['etag'],p['size'],p['sha256']) for n,p in accepted.items()))
        finally:db.close()
        self.responses=accepted

    def _abandon(self,state,journal):
        if state.get('run_id') and not state.get('conclusion'):
            try:gh(['api','repos/'+REPO+'/actions/runs/'+str(state['run_id'])+'/cancel','--method','POST'])
            except RuntimeError:pass
        # Invalidate all delegated capabilities before another client may write.
        try:self.base.abort_multipart_upload(Bucket=self.bucket,Key=self.key,UploadId=state['upload_id'])
        except Exception as error:
            if m._code(error)!='NoSuchUpload':raise RuntimeError('runner upload requires cleanup') from None
        state['aborted']=True;state['checked_at']=now();save(journal,state)

    def _prepare(self,upload_id,current_number):
        part_size,manifest,missing=self._manifest(upload_id);journal=self._journal(upload_id)
        state=json.loads(journal.read_text()) if journal.exists() else None
        if state and (state.get('sha256'),state.get('size'),state.get('upload_id'))!=(self.sha,self.size,upload_id):
            raise ValueError('runner journal identity changed')
        if state and state.get('aborted'):raise ValueError('runner upload already retired')
        if state is None:
            if current_number not in missing:raise ValueError('runner current part is not missing')
            signer=self._signer();parts=[]
            for number in missing:
                url=signer.generate_presigned_url('upload_part',Params={'Bucket':self.bucket,'Key':self.key,'UploadId':upload_id,
                    'PartNumber':number,'ContentLength':manifest[number]['size']},ExpiresIn=1800,HttpMethod='PUT')
                parts.append({'number':number,'url':url})
            spec={'sha256':self.sha,'size':self.size,'part_size':part_size,'parts':parts,
                  'source_url':self.source_url,'referer':self.referer}
            validate_spec(spec)
            head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
            if gh(['api','repos/'+REPO+'/git/ref/heads/main'])['object']['sha']!=head:raise ValueError('runner checkout does not match main')
            state={'nonce':uuid.uuid4().hex,'sha256':self.sha,'size':self.size,'upload_id':upload_id,'head_sha':head,
                   'numbers':missing,'created_at':now(),'spec':spec}
            self._mark_delegated(upload_id)
            try:save(journal,state)
            except BaseException:
                self.base.abort_multipart_upload(Bucket=self.bucket,Key=self.key,UploadId=upload_id)
                raise
        try:
            receipt=state.get('receipt') or self._execute(state,state['spec'],journal)
            self._verify_and_seed(state,receipt,manifest)
            state['receipt']=receipt;state['checked_at']=now();save(journal,state)
            print('runner accepted',sum(p['size'] for p in receipt['parts']),'bytes; host verification follows',flush=True)
        except BaseException:
            self._abandon(state,journal)
            raise

    def upload_part(self,**kwargs):
        number=kwargs['PartNumber'];upload_id=kwargs['UploadId']
        if (kwargs['Bucket'],kwargs['Key'])!=(self.bucket,self.key):raise ValueError('runner upload target changed')
        if number not in self.responses:self._prepare(upload_id,number)
        part=self.responses.get(number)
        if (not part or part['size']!=kwargs['ContentLength'] or part['sha256']!=hashlib.sha256(kwargs['Body']).hexdigest()):
            raise ValueError('runner result does not match current local payload')
        return {'ETag':part['etag']}
