import hashlib
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from urllib.error import HTTPError,URLError

import archiver as a
from test_archiver import Fetch,Response,PAYLOAD


class PartialTransportTests(unittest.TestCase):
    secure='https://web.archive.org/web/20250325170806id_/http://mirror.test/demo.zip?ver=1'
    plain=secure.replace('https://','http://',1)

    def prepare(self,folder,url,checksum=True,validator=None):
        destination=Path(folder)/'demo.zip'
        part=destination.with_name(destination.name+'.'+hashlib.sha256(url.encode()).hexdigest()[:16]+'.part')
        part.write_bytes(PAYLOAD[:5]);state=part.with_suffix('.json')
        state.write_text(json.dumps(dict(url=url,validator=validator)))
        record=dict(expected_md5=hashlib.md5(PAYLOAD).hexdigest() if checksum else None,expected_size=len(PAYLOAD))
        return destination,part,state,record

    def response(self,offset=5,headers=None):
        return Response(PAYLOAD[offset:],status=206,headers={'Content-Range':f'bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}','Content-Length':len(PAYLOAD)-offset,**(headers or {})})

    def test_refused_https_resumes_same_partial_on_http_with_publisher_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            destination,part,state,record=self.prepare(folder,self.secure)
            fetch=Fetch([URLError(ConnectionRefusedError('refused')),self.response()])
            result=a.transfer(fetch,self.secure,destination,record)
            self.assertEqual(fetch.requests,[(self.secure,{'Range':'bytes=5-'}),(self.plain,{'Range':'bytes=5-'})])
            self.assertEqual(destination.read_bytes(),PAYLOAD);self.assertEqual(result['sha256'],hashlib.sha256(PAYLOAD).hexdigest())
            self.assertFalse(part.exists());self.assertFalse(state.exists())
            self.assertEqual(list(Path(folder).glob('*.part')),[])

    def test_refused_http_upgrades_with_validator_and_preserves_if_range(self):
        with tempfile.TemporaryDirectory() as folder:
            destination,part,state,record=self.prepare(folder,self.plain,checksum=False,validator='"fixture"')
            fetch=Fetch([ConnectionRefusedError('refused'),self.response(headers={'ETag':'"fixture"'})])
            a.transfer(fetch,self.plain,destination,record)
            expected={'Range':'bytes=5-','If-Range':'"fixture"'}
            self.assertEqual(fetch.requests,[(self.plain,expected),(self.secure,expected)])
            self.assertEqual(destination.read_bytes(),PAYLOAD)

    def test_no_resume_downgrade_without_hash_or_after_http_or_certificate_failure(self):
        cases=[(False,URLError(ConnectionRefusedError('refused'))),
               (True,HTTPError(self.secure,403,'forbidden',{},None)),
               (True,URLError(ssl.SSLCertVerificationError('certificate')))]
        for checksum,error in cases:
            with self.subTest(checksum=checksum,error=type(error).__name__),tempfile.TemporaryDirectory() as folder:
                destination,part,state,record=self.prepare(folder,self.secure,checksum=checksum,validator='"fixture"')
                saved=state.read_bytes();fetch=Fetch([error])
                try:
                    with self.assertRaises(type(error)):a.transfer(fetch,self.secure,destination,record)
                    self.assertEqual(len(fetch.requests),1);self.assertEqual(part.read_bytes(),PAYLOAD[:5]);self.assertEqual(state.read_bytes(),saved)
                finally:
                    if isinstance(error,HTTPError):error.close()

    def test_invalid_alternate_range_preserves_prefix_and_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            destination,part,state,record=self.prepare(folder,self.secure)
            saved=state.read_bytes();fetch=Fetch([URLError(ConnectionRefusedError('refused')),self.response(offset=6)])
            with self.assertRaisesRegex(ValueError,'Content-Range'):a.transfer(fetch,self.secure,destination,record)
            self.assertEqual(part.read_bytes(),PAYLOAD[:5]);self.assertEqual(state.read_bytes(),saved)

    def test_interrupted_alternate_keeps_original_url_binding_for_next_resume(self):
        class Interrupted(Response):
            def read(self,size=-1):
                if self.tell():raise TimeoutError('interrupted body')
                return super().read(3)
        with tempfile.TemporaryDirectory() as folder:
            destination,part,state,record=self.prepare(folder,self.secure)
            broken=Interrupted(PAYLOAD[5:],status=206,headers={'Content-Range':f'bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}'})
            fetch=Fetch([URLError(ConnectionRefusedError('refused')),broken])
            with self.assertRaises(TimeoutError):a.transfer(fetch,self.secure,destination,record)
            self.assertEqual(part.read_bytes(),PAYLOAD[:8]);self.assertEqual(json.loads(state.read_text())['url'],self.secure)
            retry=Fetch([URLError(ConnectionRefusedError('refused')),self.response(offset=8)])
            a.transfer(retry,self.secure,destination,record)
            self.assertEqual(retry.requests,[(self.secure,{'Range':'bytes=8-'}),(self.plain,{'Range':'bytes=8-'})])
            self.assertEqual(destination.read_bytes(),PAYLOAD)
