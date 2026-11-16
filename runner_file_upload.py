"""Refetch one verified public source and send selected delegated parts only."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
import zlib

from runner_transfer_probe import validate_capabilities,MIN_PART

MAX_BYTES=5_000_000_000
MAX_SPEC=1024**2


def encode_spec(spec):
    raw=json.dumps(spec,separators=(',',':')).encode()
    if len(raw)>MAX_SPEC:raise ValueError('transfer specification too large')
    encoded=base64.b64encode(zlib.compress(raw)).decode()
    if len(encoded)>60000:raise ValueError('dispatch input too large')
    return encoded


def decode_spec(encoded):
    if not isinstance(encoded,str) or len(encoded)>60000:raise ValueError('invalid encoded specification')
    packed=base64.b64decode(encoded,validate=True);decoder=zlib.decompressobj()
    raw=decoder.decompress(packed,MAX_SPEC+1)
    if len(raw)>MAX_SPEC or not decoder.eof or decoder.unused_data:raise ValueError('invalid bounded specification')
    return json.loads(raw)


def public_source(url):
    if not isinstance(url,str) or re.search(r'[\x00-\x1f\x7f]',url):raise ValueError('invalid public source')
    p=urlsplit(url)
    if (p.scheme not in {'http','https'} or not p.hostname or p.username is not None
            or p.password is not None or p.fragment or p.port not in (None,80,443)):
        raise ValueError('invalid public source')
    host=p.hostname.lower()
    if re.search(r'\s',host):raise ValueError('invalid source hostname')
    if host=='localhost' or host.endswith(('.localhost','.local','.internal')):raise ValueError('private source')
    try:address=ipaddress.ip_address(host)
    except ValueError:pass
    else:
        if not address.is_global:raise ValueError('private source')
    return url


def validate_spec(spec):
    sha=spec.get('sha256');size=spec.get('size');part_size=spec.get('part_size');parts=spec.get('parts')
    mode=spec.get('mode','verified')
    if mode not in {'verified','staging'}:raise ValueError('invalid transfer mode')
    if mode=='verified' or sha is not None:
        if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('invalid digest')
    md5=spec.get('md5')
    if md5 is not None and (not isinstance(md5,str) or not re.fullmatch('[a-f0-9]{32}',md5)):raise ValueError('invalid MD5')
    staging_key=spec.get('staging_key')
    if mode=='staging':
        if not isinstance(staging_key,str) or not re.fullmatch('staging/[a-f0-9]{64}',staging_key):raise ValueError('invalid staging key')
    elif staging_key is not None:raise ValueError('staging key requires staging mode')
    if type(size) is not int or not 0<size<=MAX_BYTES:raise ValueError('invalid size')
    if type(part_size) is not int or not MIN_PART<=part_size<=64*1024**2:raise ValueError('invalid part size')
    published_size=spec.get('published_size')
    if published_size is not None and (type(published_size) is not int or published_size!=size):raise ValueError('published size differs from transport size')
    public_source(spec.get('source_url'))
    if spec.get('referer'):public_source(spec['referer'])
    count=(size+part_size-1)//part_size
    if not isinstance(parts,list) or not 0<len(parts)<=count:raise ValueError('invalid selected parts')
    previous=0
    for part in parts:
        number=part.get('number')
        if type(number) is not int or not previous<number<=count:raise ValueError('invalid selected part number')
        previous=number
    validate_capabilities(sha,((p['number'],p.get('url')) for p in parts),staging_key=staging_key)
    return sha,size,part_size,parts


@contextmanager
def http_response(opener,target,*,phase,**kwargs):
    """Keep source and destination failures distinguishable without leaking URLs."""
    try:
        with opener(target,**kwargs) as response:
            yield response
    except HTTPError as error:
        status=error.code if type(error.code) is int and 100<=error.code<=599 else None
        error.close()
        print('runner HTTP failure',json.dumps({'phase':phase,'status':status}),flush=True)
        raise


def transfer(spec,source_open=None,part_open=urlopen,clock=time.monotonic):
    sha,size,part_size,parts=validate_spec(spec)
    if source_open is None:
        import archiver as a
        fetcher=a.Fetcher(delay=0,timeout=60,retries=3,tls_pins=a.source_tls_pins())
        from drive_download import open_download
        source_open=lambda url,headers=None:open_download(fetcher,url,headers)
    with tempfile.TemporaryDirectory(prefix='mirror-runner-upload-') as folder:
        if shutil.disk_usage(folder).free<size+1024**3:raise OSError('runner disk reserve')
        path=Path(folder)/'body';digest=hashlib.sha256();md5=hashlib.md5();count=0;start=clock();report_at=128*1024**2
        print('runner source download started',size,'bytes',flush=True)
        headers={'Referer':spec['referer']} if spec.get('referer') else {}
        with http_response(source_open,spec['source_url'],phase='source',headers=headers) as response,path.open('wb') as target:
            if response.status!=200:raise ValueError('source HTTP status')
            if response.headers.get('Content-Encoding','identity')!='identity':raise ValueError('encoded source body')
            if 'html' in response.headers.get('Content-Type','').lower():raise ValueError('HTML source body')
            while chunk:=response.read(1024**2):
                if count==0 and chunk[:512].lstrip().lower().startswith((b'<!doctype html',b'<html',b'<head',b'<body')):raise ValueError('HTML source body')
                count+=len(chunk)
                if count>size:raise ValueError('source exceeded expected size')
                digest.update(chunk);md5.update(chunk);target.write(chunk)
                if count>=report_at:
                    print('runner source bytes received',count,'of',size,flush=True);report_at+=128*1024**2
        actual_sha=digest.hexdigest();actual_md5=md5.hexdigest()
        if count!=size or (sha is not None and actual_sha!=sha):raise ValueError('refetched source digest or size mismatch')
        if spec.get('md5') is not None and actual_md5!=spec['md5']:raise ValueError('published MD5 mismatch')
        downloaded=clock()-start;start=clock();accepted=[];acknowledged=0
        print('runner source verified',count,'bytes','download seconds',round(downloaded,3),flush=True)
        with path.open('rb') as source:
            for part in parts:
                number=part['number'];expected=min(part_size,size-(number-1)*part_size)
                source.seek((number-1)*part_size);payload=source.read(expected)
                if len(payload)!=expected:raise ValueError('staged body changed')
                request=Request(part['url'],data=payload,method='PUT',headers={'Content-Length':str(expected)})
                with http_response(part_open,request,phase='upload-part',timeout=60) as response:
                    if not 200<=response.status<300:raise ValueError('part HTTP status')
                    etag=response.headers.get('ETag')
                    if not isinstance(etag,str) or not etag:raise ValueError('part ETag missing')
                accepted.append({'number':number,'size':expected,'sha256':hashlib.sha256(payload).hexdigest(),'etag':etag})
                acknowledged+=expected
                if len(accepted)%16==0 or len(accepted)==len(parts):
                    print('runner selected parts acknowledged',len(accepted),'bytes',acknowledged,flush=True)
                del payload
        return {'sha256':actual_sha,'md5':actual_md5,'size':size,'parts':accepted,'download_seconds':round(downloaded,3),
                'upload_seconds':round(clock()-start,3),'completed_public_object':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--event',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        event=json.loads(Path(args.event).read_text());spec=decode_spec(event['inputs']['spec'])
        result=transfer(spec);result['nonce']=event['inputs']['nonce']
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
        print('runner parts accepted',len(result['parts']),'bytes',sum(p['size'] for p in result['parts']),
              'download seconds',result['download_seconds'],'upload seconds',result['upload_seconds'])
        return 0
    except Exception as error:
        print('runner file upload failed',type(error).__name__)
        return 1


if __name__=='__main__':raise SystemExit(main())
