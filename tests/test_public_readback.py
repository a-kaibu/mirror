import hashlib
import io
import re
import threading
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
import archiver as a
import public_readback as p
from test_archiver import HTML,PAYLOAD

class Reply(io.BytesIO):
    def __init__(self,data,headers,status=200,before_read=None):
        super().__init__(data);self.headers=headers;self.status=status;self.before_read=before_read
    def read(self,size=-1):
        if self.before_read:
            callback=self.before_read;self.before_read=None;callback()
        return super().read(size)

class PublicSource:
    def __init__(self,data=b'0123456789abcdefghi',headers=None,mutate=None,overlap=False):
        self.data=data;self.headers={'Content-Length':str(len(data)),'Accept-Ranges':'bytes','ETag':'"stable"'}
        if headers:
            for key,value in headers.items():
                if value is None:self.headers.pop(key,None)
                else:self.headers[key]=value
        self.mutate=mutate;self.calls=[];self.replies=[];self.barrier=threading.Barrier(2) if overlap else None
    def __call__(self,url,headers=None):
        self.calls.append(headers)
        if headers is None:
            reply=Reply(self.data,dict(self.headers))
        else:
            start,end=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',headers['Range']).groups())
            values={'Content-Range':f'bytes {start}-{end}/{len(self.data)}','Content-Length':str(end-start+1),'ETag':'"stable"'}
            data=self.data[start:end+1];status=206
            if self.mutate:values,data,status=self.mutate(values,data,status)
            def before():
                if self.barrier and start<10:
                    self.barrier.wait(2)
                    if start==0:time.sleep(.02)
            reply=Reply(data,values,status,before)
        self.replies.append(reply);return reply

class PublicReadbackTests(unittest.TestCase):
    def setUp(self):
        self.threshold=patch.object(p,'THRESHOLD',8);self.chunk=patch.object(p,'CHUNK_SIZE',5)
        self.threshold.start();self.chunk.start()
    def tearDown(self):self.chunk.stop();self.threshold.stop()
    def test_out_of_order_ranges_hash_the_complete_body_in_original_order(self):
        source=PublicSource(overlap=True)
        self.assertEqual(p.digest('https://public.test/file',source),(hashlib.sha256(source.data).hexdigest(),len(source.data)))
        ranges=[h for h in source.calls if h]
        self.assertEqual({h['Range'] for h in ranges},{'bytes=0-4','bytes=5-9','bytes=10-14','bytes=15-18'})
        self.assertTrue(all(h['If-Match']=='"stable"' and h['Accept-Encoding']=='identity' for h in ranges))
        self.assertTrue(all(r.closed for r in source.replies))
    def test_changed_or_malformed_ranges_fail_instead_of_accepting_partial_bytes(self):
        cases=[
            lambda h,b,s:({**h,'Content-Range':'bytes 1-5/19'},b,s),
            lambda h,b,s:({**h,'Content-Range':'bytes 0-4/20'},b,s),
            lambda h,b,s:({**h,'ETag':'"changed"'},b,s),
            lambda h,b,s:({**h,'Content-Length':'999'},b,s),
            lambda h,b,s:({**h,'Content-Encoding':'gzip'},b,s),
            lambda h,b,s:(h,b[:-1],s),lambda h,b,s:(h,b+b'x',s),lambda h,b,s:(h,b,204)]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                source=PublicSource(mutate=mutate)
                with self.assertRaises(ValueError):p.digest('https://public.test/file',source)
                self.assertTrue(all(r.closed for r in source.replies))
    def test_ignored_ranges_fall_back_to_one_complete_stream(self):
        source=PublicSource(mutate=lambda h,b,s:(h,b,200))
        self.assertEqual(p.digest('https://public.test/file',source),(hashlib.sha256(source.data).hexdigest(),len(source.data)))
        self.assertIsNone(source.calls[-1]);self.assertTrue(any(source.calls))
    def test_weak_missing_or_unsupported_validators_keep_the_streaming_path(self):
        for headers in [{'ETag':'W/"weak"'},{'ETag':None},{'Accept-Ranges':'none'},{'Content-Length':None}]:
            with self.subTest(headers=headers):
                source=PublicSource(headers=headers)
                self.assertEqual(p.digest('https://public.test/file',source),(hashlib.sha256(source.data).hexdigest(),len(source.data)))
                self.assertEqual(source.calls,[None])
        source=PublicSource(data=b'small')
        p.digest('https://public.test/file',source);self.assertEqual(source.calls,[None])
    def test_precondition_failure_is_not_silently_retried_as_another_version(self):
        source=PublicSource()
        def request(url,headers=None):
            if headers:raise HTTPError(url,412,'changed',{},None)
            return source(url)
        with self.assertRaises(HTTPError) as caught:p.digest('https://public.test/file',request)
        caught.exception.close()
    def test_failed_range_verification_keeps_the_unpublished_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                record=a.parse_detail(HTML,'https://source.test/items/1','lathercraft')[0];store.add(record)
                path=store.root/'files/demo.zip';path.parent.mkdir();path.write_bytes(PAYLOAD)
                store.update(record['id'],status='verified',path='files/demo.zip',**a.digest(path))
                source=PublicSource(PAYLOAD,mutate=lambda h,b,s:({**h,'ETag':'"changed"'},b,s));client=Mock()
                with patch.object(a,'s3_client',return_value=client),patch.object(a,'Fetcher',return_value=SimpleNamespace(open=source)):
                    with self.assertRaisesRegex(ValueError,'validator changed'):a.publish(store,'vn-archive','mirrors',metadata=False)
                self.assertTrue(path.exists())
                self.assertIsNone(store.rows()[0]['remote_verified_at'])
            finally:store.db.close()
