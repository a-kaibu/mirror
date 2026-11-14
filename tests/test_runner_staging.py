import hashlib
import json
import unittest
from unittest.mock import Mock
import runner_file_upload as worker
from test_archiver import PAYLOAD
from test_runner_upload import Signer, specification
from test_runner_transfer_probe import Response


def staged(body=PAYLOAD):
    value=specification(body)
    value.update(mode='staging',sha256=None,md5=hashlib.md5(body).hexdigest(),staging_key='staging/'+'b'*64)
    for part in value['parts']:
        part['url']=Signer().generate_presigned_url('upload_part',Params={
            'PartNumber':part['number'],'UploadId':'fixture','Key':value['staging_key']})
    return value


class StagedWorkerTests(unittest.TestCase):
    def test_single_origin_fetch_computes_hashes_without_local_host_digest(self):
        source=Mock(side_effect=lambda *a,**k:Response(PAYLOAD))
        send=Mock(side_effect=lambda *a,**k:Response())
        result=worker.transfer(staged(),source_open=source,part_open=send)
        source.assert_called_once();send.assert_called_once()
        self.assertEqual(result['sha256'],hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(result['md5'],hashlib.md5(PAYLOAD).hexdigest())
        self.assertEqual(result['size'],len(PAYLOAD))
        self.assertFalse(result['completed_public_object'])
        self.assertNotIn('uploadId',json.dumps(result));self.assertNotIn('X-Amz',json.dumps(result))

    def test_published_md5_and_sha_are_checked_before_any_put(self):
        for change in [dict(md5='0'*32),dict(sha256='0'*64)]:
            send=Mock()
            with self.subTest(change=change),self.assertRaises(ValueError):
                worker.transfer(dict(staged(),**change),source_open=lambda *a,**k:Response(PAYLOAD),part_open=send)
            send.assert_not_called()

    def test_hashless_source_retains_full_computed_identity(self):
        value=staged();value['md5']=None
        result=worker.transfer(value,source_open=lambda *a,**k:Response(PAYLOAD),part_open=lambda *a,**k:Response())
        self.assertEqual(result['sha256'],hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(result['md5'],hashlib.md5(PAYLOAD).hexdigest())

    def test_html_and_encoded_sources_never_reach_staging_put(self):
        cases=[(b' <html>error</html>',{}),(PAYLOAD,{'Content-Type':'text/html'}),(PAYLOAD,{'Content-Encoding':'gzip'})]
        for body,headers in cases:
            response=Response(body);response.headers.update(headers);send=Mock()
            with self.subTest(headers=headers,body=body[:8]),self.assertRaises(ValueError):
                worker.transfer(staged(body),source_open=lambda *a,**k:response,part_open=send)
            send.assert_not_called()

    def test_staging_capabilities_cannot_target_canonical_or_other_keys(self):
        for change in [dict(mode='unknown'),dict(mode='verified'),dict(staging_key='files/'+'b'*64),dict(staging_key='staging/../other'),dict(staging_key='staging/'+'B'*64),dict(published_size=len(PAYLOAD)+1),dict(md5='invalid')]:
            with self.subTest(change=change),self.assertRaises(ValueError):worker.validate_spec(dict(staged(),**change))
        value=staged();value['parts'][0]['url']=value['parts'][0]['url'].replace('/staging/','/files/')
        with self.assertRaises(ValueError):worker.validate_spec(value)
        value=staged();value['parts'][0]['url']=value['parts'][0]['url'].replace('X-Amz-Expires=1800','X-Amz-Expires=3600')
        with self.assertRaises(ValueError):worker.validate_spec(value)

    def test_short_or_oversized_source_cannot_send_any_part(self):
        for body in [PAYLOAD[:-1],PAYLOAD+b'extra']:
            send=Mock()
            with self.subTest(size=len(body)),self.assertRaises(ValueError):
                worker.transfer(staged(),source_open=lambda *a,**k:Response(body),part_open=send)
            send.assert_not_called()
