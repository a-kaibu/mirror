"""Bounded RAR4 review: decode with libarchive, compare member CRCs ourselves."""
import binascii
import ctypes as C
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

MAX_FILE=512*1024**2
MAX_EXPANDED=1024**3
MAX_ENTRIES=10000


def require(condition,message):
    if not condition:raise ValueError(message)


def member_headers(path):
    size=path.stat().st_size
    require(0<size<=MAX_FILE,'RAR input size bound')
    entries=[];ended=False;main=False
    with path.open('rb') as stream:
        require(stream.read(7)==b'Rar!\x1a\x07\x00','RAR4 signature required')
        while stream.tell()<size:
            prefix=stream.read(7)
            require(len(prefix)==7,'truncated RAR header')
            crc,kind,flags,length=struct.unpack('<HBHH',prefix)
            require(length>=7,'invalid RAR header size')
            header=prefix+stream.read(length-7)
            require(len(header)==length,'truncated RAR header body')
            require(binascii.crc32(header[2:])&0xffff==crc,'RAR header CRC mismatch')
            require(kind in (0x73,0x74,0x7b),'unsupported RAR header type')
            require(not flags&0x8000 or length>=11,'RAR packed length missing')
            packed=struct.unpack_from('<I',header,7)[0] if flags&0x8000 else 0
            if kind==0x73:
                require(not main and not entries and length>=13 and packed==0,'RAR main header order')
                require(not flags&(0x0001|0x0080),'volume/encrypted RAR unsupported')
                main=True
            elif kind==0x74:
                require(main and length>=32 and flags&0x8000,'RAR file header missing')
                require(not flags&7,'split/encrypted RAR member unsupported')
                unpacked=struct.unpack_from('<I',header,11)[0]
                checksum=struct.unpack_from('<I',header,16)[0]
                if flags&0x100:
                    require(length>=40,'large RAR header truncated')
                    packed+=struct.unpack_from('<I',header,32)[0]<<32
                    unpacked+=struct.unpack_from('<I',header,36)[0]<<32
                entries.append(dict(size=unpacked,crc=checksum))
                require(len(entries)<=MAX_ENTRIES,'RAR member count bound')
            require(stream.tell()+packed<=size,'truncated RAR packed data')
            stream.seek(packed,1)
            if kind==0x7b:
                require(main and packed==0 and stream.tell()==size,'unexpected RAR end data')
                ended=True;break
    require(ended and entries,'RAR end or members missing')
    require(sum(entry['size'] for entry in entries)<=MAX_EXPANDED,'RAR expanded size bound')
    return entries


def _verify(path):
    headers=member_headers(path);before=path.stat()
    with path.open('rb') as stream:sha256=hashlib.file_digest(stream,'sha256').hexdigest()
    library=ctypes.util.find_library('archive')
    require(library is not None,'RAR review requires libarchive')
    lib=C.CDLL(library)
    def api(name,restype,*argtypes):
        fn=getattr(lib,name);fn.restype=restype;fn.argtypes=list(argtypes);return fn
    new=api('archive_read_new',C.c_void_p)
    support=api('archive_read_support_format_rar',C.c_int,C.c_void_p)
    open_file=api('archive_read_open_filename',C.c_int,C.c_void_p,C.c_char_p,C.c_size_t)
    next_header=api('archive_read_next_header',C.c_int,C.c_void_p,C.POINTER(C.c_void_p))
    read_data=api('archive_read_data',C.c_ssize_t,C.c_void_p,C.c_void_p,C.c_size_t)
    entry_size=api('archive_entry_size',C.c_longlong,C.c_void_p)
    size_set=api('archive_entry_size_is_set',C.c_int,C.c_void_p)
    filetype=api('archive_entry_filetype',C.c_uint,C.c_void_p)
    free=api('archive_read_free',C.c_int,C.c_void_p)
    version=api('archive_version_string',C.c_char_p)
    archive=new();require(bool(archive),'RAR reader allocation failed')
    entries=total=0
    try:
        require(support(archive)==0,'RAR reader unavailable')
        require(open_file(archive,os.fsencode(path),1024*1024)==0,'RAR open failed')
        entry=C.c_void_p();buffer=C.create_string_buffer(1024*1024)
        while True:
            status=next_header(archive,C.byref(entry))
            if status==1:break
            require(status==0,'RAR header warning or failure')
            entries+=1
            require(entries<=len(headers) and bool(size_set(entry)),'RAR reader entry count/size disagreement')
            require(filetype(entry) in (0o100000,0o040000),'RAR nonregular member unsupported')
            expected=entry_size(entry)
            require(expected==headers[entries-1]['size'],'RAR reader/header size disagreement')
            read=crc=0
            while True:
                count=read_data(archive,buffer,len(buffer))
                require(count>=0,'RAR data or checksum failure')
                if count==0:break
                read+=count;require(read<=expected,'RAR member exceeds declared size')
                crc=binascii.crc32(buffer.raw[:count],crc)
            require(read==expected,'RAR member truncated')
            require(crc==headers[entries-1]['crc'],'independent RAR member CRC mismatch')
            total+=read
        require(entries==len(headers),'RAR entry count disagreement')
        after=path.stat()
        require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
            (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns),'RAR changed during validation')
        result=dict(sha256=sha256,size=before.st_size,library=version().decode(),members=entries,
            uncompressed_size=total,rar_crc='ok',extracted=False,executed=False)
    finally:close_status=free(archive)
    require(close_status==0,'RAR close warning or failure')
    return result


def verify(path):
    """Run native decoding in an isolated process with time/memory/CPU bounds."""
    require(os.name=='posix','RAR review requires POSIX resource limits')
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',str(Path(path).resolve())],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=250,check=False)
    except subprocess.TimeoutExpired as error:raise ValueError('RAR review timed out') from error
    require(result.returncode==0 and len(result.stdout)<=4096,'RAR isolated validation failed')
    value=json.loads(result.stdout)
    require(isinstance(value,dict) and value.get('rar_crc')=='ok' and value.get('extracted') is False
        and value.get('executed') is False,'RAR validator result invalid')
    return value


if __name__=='__main__':
    import argparse
    import resource
    import signal
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',required=True,type=Path)
    args=parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS,(512*1024**2,512*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU,(180,180));signal.alarm(240)
    try:print(json.dumps(_verify(args.worker)))
    except Exception as error:
        print(type(error).__name__,file=sys.stderr);raise SystemExit(1)
