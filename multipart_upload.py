"""Immutable-file uploads with durable, independently verified parts."""
import hashlib
import io
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
from pathlib import Path
import re
from datetime import datetime,timezone

from archive_lock import exclusive_file

PART_SIZE=8*1024**2
MIN_PART_SIZE=5*1024**2
MAX_PART_SIZE=64*1024**2
MAX_PARTS=10000


class _PartBody(io.BytesIO):
    def read(self,size=-1):
        # Botocore asks urllib3 for 128 KiB blocks. On a slow shared route
        # even that can exceed the connection deadline for one TLS write.
        # Short positive reads are valid stream behavior; read-all stays intact.
        if size is not None and size>16*1024:size=16*1024
        return super().read(size)


class PublicReadbackMismatch(ValueError):
    pass


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS _s3_uploads(
        namespace TEXT,bucket TEXT,key TEXT,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
        part_size INTEGER NOT NULL,upload_id TEXT NOT NULL,phase TEXT NOT NULL,
        created_at TEXT NOT NULL,PRIMARY KEY(namespace,bucket,key));
      CREATE TABLE IF NOT EXISTS _s3_upload_parts(
        namespace TEXT,bucket TEXT,key TEXT,number INTEGER,etag TEXT NOT NULL,
        size INTEGER NOT NULL,sha256 TEXT NOT NULL,
        PRIMARY KEY(namespace,bucket,key,number),
        FOREIGN KEY(namespace,bucket,key) REFERENCES _s3_uploads(namespace,bucket,key) ON DELETE CASCADE);
    """)



def cache_path(root):
    return Path(root)/'.work'/'multipart.sqlite3'


@contextmanager
def _checkpoints(root):
    path=cache_path(root);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    try:
        db.row_factory=sqlite3.Row
        with exclusive_file(Path(root)/'.claims'/'upload-checkpoint-init.lock'):
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA foreign_keys=ON');initialize(db)
        yield SimpleNamespace(root=Path(root),db=db)
    finally:db.close()


def discard(store,namespace,bucket,key,sha256):
    if not cache_path(store.root).is_file():return
    with _checkpoints(store.root) as cache:_discard(cache,namespace,bucket,key,sha256)


def transfer_and_readback(store,client,namespace,bucket,key,path,sha256,size,readback,part_size=PART_SIZE):
    if store.db.in_transaction:raise ValueError('multipart upload requires a committed SQLite connection')
    with _checkpoints(store.root) as cache:
        return _transfer_and_readback(cache,client,namespace,bucket,key,path,sha256,size,readback,part_size)


def snapshot_path(root,namespace,bucket,sha256,key='index.sqlite3'):
    slot=hashlib.sha256(json_identity(namespace,bucket,key).encode()).hexdigest()
    return Path(root)/'.work'/'snapshots'/slot/sha256/key


def json_identity(namespace,bucket,key='index.sqlite3'):
    import json
    if key not in {'index.sqlite3','index.sqlite3.gz','pages.sqlite3.gz'}:raise ValueError('invalid snapshot transport key')
    return json.dumps([namespace,bucket,key],separators=(',', ':'))


def transfer_snapshot_and_readback(store,client,namespace,bucket,path,sha256,size,readback,*,key='index.sqlite3'):
    expected=snapshot_path(store.root,namespace,bucket,sha256,key)
    if Path(path).absolute()!=expected.absolute() or any(p.is_symlink() for p in (expected.parent,expected.parent.parent,expected.parent.parent.parent,expected.parent.parent.parent.parent)):
        raise ValueError('snapshot must be an immutable managed staging file')
    if store.db.in_transaction:raise ValueError('snapshot upload requires a committed SQLite connection')
    with _checkpoints(store.root) as cache:
        return _transfer_and_readback(cache,client,namespace,bucket,key,expected,sha256,size,readback,snapshot=True)


def _code(error):
    return getattr(error,'response',{}).get('Error',{}).get('Code')


def _discard(store,namespace,bucket,key,sha256):
    identity=(namespace,bucket,key)
    with store.db:
        state=store.db.execute('SELECT sha256 FROM _s3_uploads WHERE namespace=? AND bucket=? AND key=?',identity).fetchone()
        if state and state[0]==sha256:
            store.db.execute('DELETE FROM _s3_upload_parts WHERE namespace=? AND bucket=? AND key=?',identity)
            store.db.execute('DELETE FROM _s3_uploads WHERE namespace=? AND bucket=? AND key=?',identity)


def _parts(client,bucket,key,upload_id):
    result={};marker=0
    while True:
        args={'Bucket':bucket,'Key':key,'UploadId':upload_id,'MaxParts':1000}
        if marker:args['PartNumberMarker']=marker
        response=client.list_parts(**args)
        for part in response.get('Parts',[]):
            number=part.get('PartNumber');size=part.get('Size');etag=part.get('ETag')
            if (type(number) is not int or not marker<number<=MAX_PARTS or number in result
                    or type(size) is not int or size<=0 or not isinstance(etag,str) or not etag):
                raise ValueError('invalid remote multipart listing')
            result[number]=part
        if not response.get('IsTruncated'):return result
        next_marker=response.get('NextPartNumberMarker')
        if type(next_marker) is not int or next_marker<=marker or next_marker>MAX_PARTS:
            raise ValueError('invalid multipart pagination')
        marker=next_marker


def _object_exists(client,bucket,key,size):
    try:return client.head_object(Bucket=bucket,Key=key).get('ContentLength')==size
    except Exception as error:
        if _code(error) in {'404','NoSuchKey','NotFound'}:return False
        raise


def _snapshot_parts(store,client,identity,path,sha256,size,state,remote,part_size):
    """Two bounded payloads in flight; only this owning thread checkpoints."""
    from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED,ALL_COMPLETED
    namespace,bucket,key=identity
    count=(size+part_size-1)//part_size
    parts={};pending={};failure=None;assembled=hashlib.sha256()
    pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='catalogue-part')
    def send_part(number,expected,payload):
        # Raw bytes become one large TLS write under the connection timeout.
        # A seekable stream gives urllib3 small writes and SDK retry rewinds.
        with _PartBody(payload) as body:
            return client.upload_part(Bucket=bucket,Key=key,UploadId=state['upload_id'],
                PartNumber=number,Body=body,ContentLength=expected)
    def drain(all_parts=False):
        nonlocal failure
        while pending:
            done,_=wait(tuple(pending),return_when=ALL_COMPLETED if all_parts else FIRST_COMPLETED)
            for future in done:
                number,expected,local_sha=pending.pop(future)
                try:
                    response=future.result();etag=response.get('ETag')
                    if not isinstance(etag,str) or not etag:raise ValueError('missing multipart part ETag')
                    with store.db:
                        store.db.execute('INSERT OR REPLACE INTO _s3_upload_parts VALUES(?,?,?,?,?,?,?)',(*identity,number,etag,expected,local_sha))
                    parts[number]={'PartNumber':number,'ETag':etag}
                except Exception as error:
                    if failure is None:failure=error
            if not all_parts:break
    try:
        with path.open('rb') as stream:
            for number in range(1,count+1):
                if len(pending)>=2:
                    drain()
                    if failure is not None:raise failure
                expected=min(part_size,size-(number-1)*part_size);payload=stream.read(expected)
                if len(payload)!=expected:raise ValueError('multipart local body shortened')
                assembled.update(payload);local_sha=hashlib.sha256(payload).hexdigest()
                saved=store.db.execute('SELECT * FROM _s3_upload_parts WHERE namespace=? AND bucket=? AND key=? AND number=?',(*identity,number)).fetchone()
                observed=remote.get(number)
                reuse=(saved and observed and saved['sha256']==local_sha and saved['size']==expected
                    and observed['Size']==expected and observed['ETag']==saved['etag'])
                if reuse:parts[number]={'PartNumber':number,'ETag':saved['etag']}
                else:
                    future=pool.submit(send_part,number,expected,payload)
                    pending[future]=(number,expected,local_sha)
                del payload
            if stream.read(1):raise ValueError('multipart local body grew')
        if assembled.hexdigest()!=sha256:raise ValueError('multipart body changed during transfer')
    finally:
        # A failed sibling must not discard successful acknowledgements or
        # leave a background sender running after releasing the object claim.
        try:drain(all_parts=True)
        finally:pool.shutdown(wait=True)
    if failure is not None:raise failure
    if len(parts)!=count:raise ValueError('missing snapshot part acknowledgement')
    return [parts[number] for number in range(1,count+1)]


def _transfer_and_readback(store,client,namespace,bucket,key,path,sha256,size,readback,part_size=PART_SIZE,*,snapshot=False):
    """Return a full public digest; never update source records or evict bodies."""
    path=Path(path)
    if (not isinstance(sha256,str) or not re.fullmatch('[a-f0-9]{64}',sha256)
            or (key!='files/'+sha256 and not (snapshot and key in {'index.sqlite3','index.sqlite3.gz','pages.sqlite3.gz'})) or type(size) is not int or size<=0
            or type(part_size) is not int or not MIN_PART_SIZE<=part_size<=MAX_PART_SIZE
            or ((size+part_size-1)//part_size)>MAX_PARTS):
        raise ValueError('invalid immutable multipart upload parameters')
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=size:
        raise ValueError('multipart local body unavailable or size changed')
    if store.db.in_transaction:raise ValueError('multipart upload requires a committed SQLite connection')
    identity=(namespace,bucket,key)
    claim=hashlib.sha256(('s3-upload\n'+'\n'.join(identity)).encode()).hexdigest()
    with exclusive_file(store.root/'.claims'/('upload-'+claim+'.lock'),blocking=False):
        # Bind retries to actual bytes, independently of a path or mtime.
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            while chunk:=stream.read(1024**2):digest.update(chunk)
        if digest.hexdigest()!=sha256:raise ValueError('multipart local SHA-256 changed')
        state=store.db.execute('SELECT * FROM _s3_uploads WHERE namespace=? AND bucket=? AND key=?',identity).fetchone()
        if state and (state['sha256']!=sha256 or state['size']!=size or state['part_size']!=part_size):
            raise ValueError('multipart checkpoint identity changed')
        remote={};completed=False
        if state:
            completed=state['phase']=='completed'
            if completed:
                completed=_object_exists(client,bucket,key,size)
                if not completed:_discard(store,*identity,sha256);state=None
            else:
                try:remote=_parts(client,bucket,key,state['upload_id'])
                except Exception as error:
                    if _code(error)!='NoSuchUpload':raise
                    completed=_object_exists(client,bucket,key,size)
                    if not completed:_discard(store,*identity,sha256);state=None
        if state and not completed and state['phase']=='delegated' and getattr(client,'delegated_uploads',False) is not True:
            raise BlockingIOError('delegated upload requires runner resume or cleanup')
        if state is None:
            response=client.create_multipart_upload(Bucket=bucket,Key=key,Metadata={'sha256':sha256})
            upload_id=response.get('UploadId')
            if not isinstance(upload_id,str) or not upload_id:raise ValueError('missing multipart upload ID')
            with store.db:
                store.db.execute('INSERT INTO _s3_uploads VALUES(?,?,?,?,?,?,?,?,?)',(*identity,sha256,size,part_size,upload_id,'active',datetime.now(timezone.utc).isoformat()))
            state=store.db.execute('SELECT * FROM _s3_uploads WHERE namespace=? AND bucket=? AND key=?',identity).fetchone()
        if not completed:
            count=((size+part_size-1)//part_size)
            if any(number>count for number in remote):raise ValueError('unexpected remote multipart part')
            if snapshot:
                parts=_snapshot_parts(store,client,identity,path,sha256,size,state,remote,part_size)
            else:
                parts=[];assembled=hashlib.sha256()
                with path.open('rb') as stream:
                    for number in range(1,count+1):
                        expected=min(part_size,size-(number-1)*part_size);payload=stream.read(expected)
                        if len(payload)!=expected:raise ValueError('multipart local body shortened')
                        assembled.update(payload)
                        local_sha=hashlib.sha256(payload).hexdigest()
                        saved=store.db.execute('SELECT * FROM _s3_upload_parts WHERE namespace=? AND bucket=? AND key=? AND number=?',(*identity,number)).fetchone()
                        observed=remote.get(number)
                        reuse=(saved and observed and saved['sha256']==local_sha and saved['size']==expected
                               and observed['Size']==expected and observed['ETag']==saved['etag'])
                        if reuse:etag=saved['etag']
                        else:
                            # A raw bytes body becomes one TLS sendall. Share the
                            # bounded, seekable stream used by snapshots so slow
                            # routes can finish a part without one giant write.
                            with _PartBody(payload) as body:
                                response=client.upload_part(Bucket=bucket,Key=key,UploadId=state['upload_id'],PartNumber=number,Body=body,ContentLength=expected)
                            etag=response.get('ETag')
                            if not isinstance(etag,str) or not etag:raise ValueError('missing multipart part ETag')
                            with store.db:
                                store.db.execute('INSERT OR REPLACE INTO _s3_upload_parts VALUES(?,?,?,?,?,?,?)',(*identity,number,etag,expected,local_sha))
                        parts.append({'PartNumber':number,'ETag':etag})
                        del payload
                    if stream.read(1):raise ValueError('multipart local body grew')
                if assembled.hexdigest()!=sha256:raise ValueError('multipart body changed during transfer')
            client.complete_multipart_upload(Bucket=bucket,Key=key,UploadId=state['upload_id'],MultipartUpload={'Parts':parts})
        with store.db:
            store.db.execute("UPDATE _s3_uploads SET phase='completed' WHERE namespace=? AND bucket=? AND key=?",identity)
        actual=readback()
        if actual!=(sha256,size):
            _discard(store,*identity,sha256)
            raise PublicReadbackMismatch('public read-back mismatch: '+key)
        # Keep completion until the caller's source-record CAS accepts proof.
        return actual
