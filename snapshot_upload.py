"""Keep one immutable catalogue snapshot across interrupted canonical uploads."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

import archiver as a
import multipart_upload as m
from archive_lock import exclusive_file


def slot(root,namespace,bucket):
    return m.snapshot_path(root,namespace,bucket,'unused').parent.parent


def has_pending(root,namespace,bucket):return (slot(root,namespace,bucket)/'current.json').is_file()


def atomic_json(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value),encoding='utf-8');temporary.replace(path)


def head(client,bucket):
    try:
        result=client.head_object(Bucket=bucket,Key='index.sqlite3')
        return result.get('ETag'),(result.get('Metadata') or {}).get('sha256')
    except Exception as error:
        if m._code(error) in {'404','NoSuchKey','NotFound'}:return None,None
        raise


def discard_local(root,namespace,bucket,state):
    path=m.snapshot_path(root,namespace,bucket,state['sha256'])
    if path.is_symlink() or path.parent.is_symlink():raise ValueError('unsafe snapshot cache path')
    path.unlink(missing_ok=True)
    if path.parent.exists():path.parent.rmdir()


def cleanup_orphans(directory):
    # Only derivative SQLite backups in this destination's managed SHA slots.
    # A crash before the atomic pointer write must not accumulate generations.
    for child in directory.iterdir():
        if (child.is_symlink() or not child.is_dir() or len(child.name)!=64
                or any(c not in '0123456789abcdef' for c in child.name)):continue
        entries=list(child.iterdir())
        if len(entries)==1 and entries[0].name=='index.sqlite3' and entries[0].is_file() and not entries[0].is_symlink():
            entries[0].unlink();child.rmdir()


def publish(store,client,namespace,bucket):
    directory=slot(store.root,namespace,bucket)
    if any(p.is_symlink() for p in (directory,directory.parent,directory.parent.parent)):
        raise ValueError('unsafe snapshot cache directory')
    directory.mkdir(parents=True,exist_ok=True)
    identity=hashlib.sha256(m.json_identity(namespace,bucket).encode()).hexdigest()
    with exclusive_file(store.root/'.claims'/('snapshot-'+identity+'.lock'),blocking=False):
        statefile=directory/'current.json';state=None
        if statefile.exists():
            state=json.loads(statefile.read_text(encoding='utf-8'))
            if (state.get('namespace'),state.get('bucket'),state.get('key'))!=(namespace,bucket,'index.sqlite3'):
                raise ValueError('snapshot cache identity changed')
            sha=state.get('sha256')
            if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):
                raise ValueError('invalid snapshot cache digest')
        remote_etag,remote_sha=head(client,bucket);already_verified=False
        if state and remote_etag is not None and remote_etag!=state['base_etag'] and remote_sha is None:
            # Some S3-compatible servers omit user metadata. A completion may
            # have succeeded before the process lost its response; only full
            # anonymous read-back can distinguish it from another generation.
            already_verified=a.public_digest(namespace,bucket,'index.sqlite3')==(state['sha256'],state['size'])
        if state and remote_etag!=state['base_etag'] and remote_sha!=state['sha256'] and not already_verified:
            # Another publisher advanced the canonical key while this pass was
            # stopped. Recreate from current metadata instead of restoring an
            # older catalogue. Never abort a remote upload belonging to others.
            m.discard(store,namespace,bucket,'index.sqlite3',state['sha256'])
            statefile.unlink();discard_local(store.root,namespace,bucket,state);state=None
        if state is None:
            cleanup_orphans(directory)
            # Missing pointer means no generation can be resumed safely. Its
            # remote multipart upload may expire; no source file is removed.
            if m.cache_path(store.root).exists():
                with m._checkpoints(store.root) as cache:
                    orphan=cache.db.execute('SELECT sha256 FROM _s3_uploads WHERE namespace=? AND bucket=? AND key=?',(namespace,bucket,'index.sqlite3')).fetchone()
                    if orphan:m._discard(cache,namespace,bucket,'index.sqlite3',orphan[0])
            size=store.db.execute('PRAGMA page_count').fetchone()[0]*store.db.execute('PRAGMA page_size').fetchone()[0]
            if shutil.disk_usage(directory).free < 3*size+1024**3:
                raise OSError('disk reserve reached before snapshot staging')
            store.export()
            with tempfile.TemporaryDirectory(prefix='build-',dir=directory) as temporary:
                built=Path(temporary)/'index.sqlite3'
                with closing(sqlite3.connect(built)) as backup:
                    store.db.backup(backup);a._compact_snapshot(backup)
                expected=a.digest(built)
                path=m.snapshot_path(store.root,namespace,bucket,expected['sha256']);path.parent.mkdir(exist_ok=True)
                if path.is_symlink():raise ValueError('unsafe snapshot cache path')
                built.replace(path)
            state=dict(namespace=namespace,bucket=bucket,key='index.sqlite3',sha256=expected['sha256'],size=expected['size'],created_at=a.now(),base_etag=remote_etag)
            atomic_json(statefile,state)
        path=m.snapshot_path(store.root,namespace,bucket,state['sha256'])
        if not already_verified:
            m.transfer_snapshot_and_readback(store,client,namespace,bucket,path,state['sha256'],state['size'],
                                             lambda:a.public_digest(namespace,bucket,'index.sqlite3'))
        receipt=dict(state,verified_at=a.now())
        atomic_json(directory/'last-verified.json',receipt)
        m.discard(store,namespace,bucket,'index.sqlite3',state['sha256'])
        statefile.unlink();discard_local(store.root,namespace,bucket,state)
        print('catalog checkpoint published; durable transfer verified',flush=True)
