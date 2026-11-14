"""Bounded deterministic gzip transport for immutable SQLite catalogue backups."""
from contextlib import closing
import gzip
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile

CHUNK=1024*1024
RESERVE=1024**3
MAX_SIZE=16*1024**3


def validate(path):
    with Path(path).open("rb") as source:
        if source.read(16)!=b"SQLite format 3\x00":raise ValueError("missing SQLite catalogue header")
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro&immutable=1",uri=True)) as db:
        result=db.execute("PRAGMA quick_check").fetchall()
        if result!=[("ok",)]:raise ValueError("invalid SQLite catalogue")


def _destination(path):
    path=Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("unsafe catalogue destination")
    path.parent.mkdir(parents=True,exist_ok=True)
    return path


def compress(source,destination,*,reserve=RESERVE):
    """Encode an already frozen backup; no timestamps or local names in gzip."""
    if reserve<0:raise ValueError("invalid catalogue compression bounds")
    source=Path(source);destination=_destination(destination)
    if source.resolve()==destination.resolve():raise ValueError("source equals destination")
    validate(source)
    raw_sha=hashlib.sha256();size=0
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(prefix=".catalogue-",dir=destination.parent,delete=False) as output:
            temporary=Path(output.name)
            with source.open("rb") as incoming,gzip.GzipFile(filename="",mode="wb",compresslevel=1,mtime=0,fileobj=output) as encoded:
                while data:=incoming.read(CHUNK):
                    if shutil.disk_usage(destination.parent).free<len(data)+reserve:
                        raise OSError("disk reserve reached during catalogue compression")
                    raw_sha.update(data);size+=len(data);encoded.write(data)
        compressed_sha=hashlib.sha256()
        with temporary.open("rb") as encoded:
            while data:=encoded.read(CHUNK):compressed_sha.update(data)
        receipt={"raw_sha256":raw_sha.hexdigest(),"raw_size":size,
                 "sha256":compressed_sha.hexdigest(),"size":temporary.stat().st_size}
        temporary.replace(destination)
        return receipt
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def restore(stream,destination,*,max_size=MAX_SIZE,reserve=RESERVE):
    """Consume gzip with CRC, bounds and SQLite checks before atomic replacement."""
    if max_size<=0 or reserve<0:raise ValueError("invalid catalogue restore bounds")
    destination=_destination(destination);temporary=None;sha=hashlib.sha256();size=0
    try:
        with tempfile.NamedTemporaryFile(prefix=".catalogue-",dir=destination.parent,delete=False) as output:
            temporary=Path(output.name)
            with gzip.GzipFile(fileobj=stream,mode="rb") as incoming:
                while data:=incoming.read(min(CHUNK,max_size-size+1)):
                    size+=len(data)
                    if size>max_size:raise ValueError("catalogue exceeds restore bound")
                    if shutil.disk_usage(destination.parent).free<len(data)+reserve:
                        raise OSError("disk reserve reached during catalogue restore")
                    sha.update(data);output.write(data)
        validate(temporary)
        temporary.replace(destination)
        return {"sha256":sha.hexdigest(),"size":size}
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
