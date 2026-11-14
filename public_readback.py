"""Bounded parallel public read-back, pinned to one strong HTTP validator."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import time
from urllib.error import HTTPError
import re

THRESHOLD=64*1024*1024
CHUNK_SIZE=16*1024*1024
WORKERS=2
RETRIES=2


class RangeUnsupported(ValueError):
    pass


class RangeIncomplete(ValueError):
    pass


def _proof(sha,md5,size):
    if md5 is None:return sha.hexdigest(),size
    return dict(sha256=sha.hexdigest(),md5=md5.hexdigest(),size=size)


def sequential(response,*,include_md5=False):
    sha=hashlib.sha256();md5=hashlib.md5() if include_md5 else None;size=0
    while chunk:=response.read(1024*1024):
        sha.update(chunk);size+=len(chunk)
        if md5 is not None:md5.update(chunk)
    return _proof(sha,md5,size)


def ranged(url,size,etag,open_response,*,include_md5=False):
    def fetch_once(start):
        end=min(size,start+CHUNK_SIZE)-1
        with open_response(url,{'Range':f'bytes={start}-{end}','If-Match':etag,'Accept-Encoding':'identity'}) as response:
            if response.status==200:raise RangeUnsupported('public server ignored Range')
            if response.status!=206:raise ValueError('unexpected public range status')
            match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
            if not match or tuple(map(int,match.groups()))!=(start,end,size):raise ValueError('public Content-Range mismatch')
            if response.headers.get('ETag')!=etag:raise ValueError('public range validator changed')
            if response.headers.get('Content-Encoding','identity')!='identity':raise ValueError('encoded public range')
            expected=end-start+1;length=response.headers.get('Content-Length')
            if length is not None and int(length)!=expected:raise ValueError('public range length mismatch')
            chunk=response.read(expected+1)
            if len(chunk)<expected:raise RangeIncomplete('public range body truncated')
            if len(chunk)>expected:raise ValueError('public range body length mismatch')
            return chunk
    def fetch(start):
        for attempt in range(RETRIES+1):
            try:return fetch_once(start)
            except HTTPError as error:
                error.close();raise
            except (TimeoutError,ConnectionResetError,ConnectionAbortedError,http.client.IncompleteRead,http.client.RemoteDisconnected,RangeIncomplete):
                if attempt>=RETRIES:raise
                # Keep the same exact range and strong validator. Only a complete
                # response advances the ordered full-body hash.
                time.sleep(min(2**attempt,10))
    sha=hashlib.sha256();md5=hashlib.md5() if include_md5 else None;total=0;offsets=iter(range(0,size,CHUNK_SIZE))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending=deque()
        for _ in range(WORKERS):
            start=next(offsets,None)
            if start is not None:pending.append(pool.submit(fetch,start))
        while pending:
            chunk=pending.popleft().result();sha.update(chunk);total+=len(chunk)
            if md5 is not None:md5.update(chunk)
            del chunk
            start=next(offsets,None)
            if start is not None:pending.append(pool.submit(fetch,start))
    if total!=size:raise ValueError('public range coverage mismatch')
    return _proof(sha,md5,total)


def _digest(url,open_response,*,include_md5=False):
    with open_response(url) as response:
        length=response.headers.get('Content-Length','')
        size=int(length) if str(length).isdigit() else 0
        etag=response.headers.get('ETag','')
        strong=len(etag)>=2 and etag.startswith('"') and etag.endswith('"')
        if not (response.status==200 and size>=THRESHOLD and strong
                and response.headers.get('Accept-Ranges','').lower()=='bytes'
                and response.headers.get('Content-Encoding','identity')=='identity'):
            return sequential(response,include_md5=include_md5)
    try:return ranged(url,size,etag,open_response,include_md5=include_md5)
    except RangeUnsupported:
        # Some intermediaries advertise ranges but return the complete object.
        # A fresh whole-body read retains the same final SHA-256/size contract.
        with open_response(url) as response:return sequential(response,include_md5=include_md5)


def digest(url,open_response):
    """Keep the established (SHA-256, size) public-proof API."""
    return _digest(url,open_response)


def digest_hashes(url,open_response):
    """Compute MD5, SHA-256 and size in one ordered public body read."""
    return _digest(url,open_response,include_md5=True)
