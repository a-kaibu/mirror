"""Finite transfer diagnostic using single-upload part capabilities, never credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit,parse_qs,unquote
from urllib.request import Request,urlopen

MAX_BYTES=64*1024**2
MIN_PART=5*1024**2


def validate_spec(spec):
    sha=spec.get('sha256');size=spec.get('size');part_size=spec.get('part_size')
    if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('invalid digest')
    if type(size) is not int or not 0<size<=MAX_BYTES:raise ValueError('invalid size')
    if type(part_size) is not int or not MIN_PART<=part_size<=MAX_BYTES:raise ValueError('invalid part size')
    source='https://huggingface.co/buckets/vn-archive/mirrors/resolve/files/'+sha
    if spec.get('source_url')!=source:raise ValueError('invalid diagnostic source')
    urls=spec.get('part_urls')
    if not isinstance(urls,list) or len(urls)!=(size+part_size-1)//part_size:raise ValueError('invalid part count')
    upload_id=None
    for number,url in enumerate(urls,1):
        if not isinstance(url,str):raise ValueError('invalid capability')
        parts=urlsplit(url);query=parse_qs(parts.query)
        if (parts.scheme!='https' or parts.hostname!='vn-archive.s3.hf.co' or parts.port not in (None,443)
                or parts.username is not None or parts.password is not None or parts.fragment
                or unquote(parts.path)!='/mirrors/files/'+sha):raise ValueError('invalid upload endpoint')
        if query.get('partNumber')!=[str(number)] or len(query.get('uploadId',[]))!=1:raise ValueError('invalid part identity')
        if upload_id is None:upload_id=query['uploadId'][0]
        if query['uploadId'][0]!=upload_id or not upload_id:raise ValueError('mixed uploads')
        if query.get('X-Amz-SignedHeaders')!=['content-length;host']:raise ValueError('unbounded capability')
        expires=query.get('X-Amz-Expires',[])
        if len(expires)!=1 or not expires[0].isdigit() or not 0<int(expires[0])<=1800:raise ValueError('invalid expiry')
        signature=query.get('X-Amz-Signature',[])
        if len(signature)!=1 or not re.fullmatch('[a-f0-9]{64}',signature[0]):raise ValueError('invalid signature')
    return sha,size,part_size,urls


def run_probe(spec,open_url=urlopen,clock=time.monotonic):
    sha,size,part_size,urls=validate_spec(spec)
    with tempfile.TemporaryDirectory(prefix='mirror-transfer-probe-') as folder:
        path=Path(folder)/'body';digest=hashlib.sha256();count=0;start=clock()
        request=Request(spec['source_url'],headers={'User-Agent':'VN-Mirror-Transfer-Probe/0.1'})
        with open_url(request,timeout=30) as response,path.open('wb') as target:
            if response.status!=200:raise ValueError('source HTTP status')
            while chunk:=response.read(1024**2):
                count+=len(chunk)
                if count>size:raise ValueError('source exceeded expected size')
                digest.update(chunk);target.write(chunk)
        if (digest.hexdigest(),count)!=(sha,size):raise ValueError('source digest or size mismatch')
        downloaded=clock()-start;start=clock();accepted=[]
        with path.open('rb') as source:
            for number,url in enumerate(urls,1):
                expected=min(part_size,size-(number-1)*part_size);payload=source.read(expected)
                if len(payload)!=expected:raise ValueError('staged body changed')
                request=Request(url,data=payload,method='PUT',headers={'Content-Length':str(expected)})
                with open_url(request,timeout=60) as response:
                    if not 200<=response.status<300:raise ValueError('part HTTP status')
                    etag=response.headers.get('ETag')
                    if not isinstance(etag,str) or not etag:raise ValueError('part ETag missing')
                accepted.append({'number':number,'size':expected,'etag':etag,'sha256':hashlib.sha256(payload).hexdigest()})
                del payload
            if source.read(1):raise ValueError('staged body grew')
        return {'sha256':sha,'size':size,'download_seconds':round(downloaded,3),
                'upload_seconds':round(clock()-start,3),'parts':accepted,'completed_public_object':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--event',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        event=json.loads(Path(args.event).read_text());spec=json.loads(event['inputs']['spec'])
        result=run_probe(spec);result['nonce']=event['inputs']['nonce']
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
        print('transfer probe parts accepted',len(result['parts']),'bytes',result['size'],'upload seconds',result['upload_seconds'])
        return 0
    except Exception as error:
        # Capability URLs must never appear in public logs or tracebacks.
        print('transfer probe failed',type(error).__name__)
        return 1


if __name__=='__main__':raise SystemExit(main())
