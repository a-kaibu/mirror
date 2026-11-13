import hashlib
import http.client
import json
import tempfile
import unittest
from urllib.error import URLError
import archiver as a
from test_archiver import Fetch, Response, HTML, PAYLOAD


class Interrupted(Response):
    def __init__(self,data,error=None,status=200,headers=None):
        super().__init__(data,status,headers)
        self.error=error or TimeoutError('timed out')
        self.returned=False

    def read(self,size=-1):
        if self.returned:raise self.error
        self.returned=True
        return super().read(size)


class StreamResumeRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=a.Store(self.tmp.name);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://example.test/items/1','lathercraft')[0]
        self.first,self.second=self.record['mirrors']

    def interrupted(self,validator=True):
        headers={'Content-Length':len(PAYLOAD)}
        if validator:headers['ETag']='"v1"'
        return Interrupted(PAYLOAD[:5],headers=headers)

    def remaining(self,data=None,validator=True):
        headers={'Content-Range':f'bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}','Content-Length':len(PAYLOAD)-5}
        if validator:headers['ETag']='"v1"'
        return Response(PAYLOAD[5:] if data is None else data,206,headers)

    def run_download(self,responses):
        self.store.add(self.record);fetch=Fetch(responses)
        self.assertEqual(a.download(self.store,fetch,'lathercraft',export=False),0)
        row=self.store.rows()[0]
        self.assertEqual(row['status'],'verified')
        self.assertEqual((self.store.root/row['path']).read_bytes(),PAYLOAD)
        return fetch

    def test_timeout_resumes_same_url_and_records_interruption(self):
        fetch=self.run_download([self.interrupted(),self.remaining()])
        self.assertEqual([u for u,h in fetch.requests],[self.first,self.first])
        self.assertEqual(fetch.requests[1][1],{'Range':'bytes=5-','If-Range':'"v1"'})
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT error FROM attempts ORDER BY id')],['timed out',None])

    def test_published_digest_allows_retry_without_validator(self):
        self.record['expected_md5']=None
        self.record['expected_sha256']=hashlib.sha256(PAYLOAD).hexdigest()
        fetch=self.run_download([self.interrupted(False),self.remaining(validator=False)])
        self.assertEqual(fetch.requests[1],(self.first,{'Range':'bytes=5-'}))

    def test_validator_allows_retry_without_published_digest(self):
        self.record['expected_md5']=None;self.record['expected_sha256']=None
        fetch=self.run_download([self.interrupted(),self.remaining()])
        self.assertEqual(fetch.requests[1][1],{'Range':'bytes=5-','If-Range':'"v1"'})

    def test_hashless_unvalidated_partial_uses_next_mirror(self):
        self.record['expected_md5']=None;self.record['expected_sha256']=None
        fetch=self.run_download([self.interrupted(False),Response(PAYLOAD)])
        self.assertEqual([u for u,h in fetch.requests],[self.first,self.second])

    def test_second_interruption_falls_back_and_keeps_partial(self):
        second=Interrupted(PAYLOAD[5:8],TimeoutError('timed out'),206,
            {'Content-Range':f'bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}','ETag':'"v1"'})
        fetch=self.run_download([self.interrupted(),second,Response(PAYLOAD)])
        self.assertEqual([u for u,h in fetch.requests],[self.first,self.first,self.second])
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT error FROM attempts ORDER BY id')],['timed out','timed out',None])
        part=next(self.store.root.rglob('*.part'))
        self.assertEqual(part.read_bytes(),PAYLOAD[:8])

    def test_changed_validator_is_rejected_without_another_resume(self):
        remaining=self.remaining();remaining.headers.replace_header('ETag','"v2"')
        fetch=self.run_download([self.interrupted(),remaining,Response(PAYLOAD)])
        self.assertEqual([u for u,h in fetch.requests],[self.first,self.first,self.second])
        self.assertIn('validator changed',self.store.db.execute('SELECT error FROM attempts ORDER BY id LIMIT 1 OFFSET 1').fetchone()[0])

    def test_corrupt_resumed_bytes_fail_checksum_and_use_next_mirror(self):
        fetch=self.run_download([self.interrupted(),self.remaining(b'x'*(len(PAYLOAD)-5)),Response(PAYLOAD)])
        self.assertEqual([u for u,h in fetch.requests],[self.first,self.first,self.second])
        self.assertIn('MD5 mismatch',self.store.db.execute('SELECT error FROM attempts ORDER BY id LIMIT 1 OFFSET 1').fetchone()[0])
        self.assertEqual(list(self.store.root.rglob('*.part')),[])

    def test_retry_policy_requires_bound_partial_and_transient_stream_error(self):
        destination=self.store.root/'demo.zip'
        part=destination.with_name(destination.name+'.'+hashlib.sha256(self.first.encode()).hexdigest()[:16]+'.part')
        part.write_bytes(PAYLOAD[:5]);receipt=part.with_suffix('.json')
        receipt.write_text(json.dumps({'url':self.first,'validator':'"v1"'}))
        for error in [TimeoutError(),ConnectionResetError(),http.client.IncompleteRead(b'x'),URLError(TimeoutError())]:
            self.assertTrue(a.resumable_interruption(error,self.first,destination,self.record))
        for error in [OSError('disk reserve reached'),ValueError('checksum'),URLError('offline')]:
            self.assertFalse(a.resumable_interruption(error,self.first,destination,self.record))
        for data in ['invalid',json.dumps({'url':self.second,'validator':'"v1"'}),'[]']:
            receipt.write_text(data)
            self.assertFalse(a.resumable_interruption(TimeoutError(),self.first,destination,self.record))
        receipt.write_text(json.dumps({'url':self.first,'validator':'"v1"'}));part.write_bytes(b'')
        self.assertFalse(a.resumable_interruption(TimeoutError(),self.first,destination,self.record))
