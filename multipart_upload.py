"""Immutable-file uploads with durable, independently verified parts."""
import hashlib
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


def snapshot_path(root,namespace,bucket,sha256):
    slot=hashlib.sha256(json_identity(namespace,bucket).encode()).hexdigest()
    return Path(root)/'.work'/'snapshots'/slot/sha256/'index.sqlite3'


def json_identity(namespace,bucket):
    import json
    return json.dumps([namespace,bucket,'index.sqlite3'],separators=(',', ':'))


def transfer_snapshot_and_readback(store,client,namespace,bucket,path,sha256,size,readback):
    expected=snapshot_path(store.root,namespace,bucket,sha256)
    if Path(path).absolute()!=expected.absolute() or any(p.is_symlink() for p in (expected.parent,expected.parent.parent,expected.parent.parent.parent,expected.parent.parent.parent.parent)):
        raise ValueError('snapshot must be an immutable managed staging file')
    if store.db.in_transaction:raise ValueError('snapshot upload requires a committed SQLite connection')
    with _checkpoints(store.root) as cache:
        return _transfer_and_readback(cache,client,namespace,bucket,'index.sqlite3',expected,sha256,size,readback,snapshot=True)


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


def _transfer_and_readback(store,client,namespace,bucket,key,path,sha256,size,readback,part_size=PART_SIZE,*,snapshot=False):
    """Return a full public digest; never update source records or evict bodies."""
    path=Path(path)
    if (not isinstance(sha256,str) or not re.fullmatch('[a-f0-9]{64}',sha256)
            or (key!='files/'+sha256 and not (snapshot and key=='index.sqlite3')) or type(size) is not int or size<=0
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
                        response=client.upload_part(Bucket=bucket,Key=key,UploadId=state['upload_id'],PartNumber=number,Body=payload,ContentLength=expected)
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
