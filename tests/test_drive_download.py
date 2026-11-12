import tempfile
from pathlib import Path
import unittest
import archiver as a
from drive_download import open_download,file_id
from test_archiver import Fetch,Response,PAYLOAD

IDENT='10x7qGPrMHPvJuzNQB1iFa_rDqlYifYSB'
URL='https://drive.google.com/file/d/'+IDENT+'/view?usp=sharing'

class DriveDownloadTests(unittest.TestCase):
    def test_public_form_then_binary_retains_range_headers(self):
        form=f'<form id="download-form" action="https://drive.usercontent.google.com/download"><input name="id" value="{IDENT}"><input name="export" value="download"><input name="confirm" value="t"><input name="uuid" value="published-token"></form>'.encode()
        fetch=Fetch([Response(form,headers={'Content-Type':'text/html'},url='https://drive.usercontent.google.com/download'),Response(PAYLOAD,headers={'Content-Type':'application/zip'},url='https://drive.usercontent.google.com/download')])
        with open_download(fetch,URL,{'Range':'bytes=10-'}) as response:self.assertEqual(response.read(),PAYLOAD)
        self.assertEqual(len(fetch.requests),2)
        self.assertIn('uuid=published-token',fetch.requests[1][0])
        self.assertEqual(fetch.requests[1][1]['Range'],'bytes=10-')

    def test_external_form_and_signin_page_are_rejected(self):
        html=f'<form id="download-form" action="https://evil.test/download"><input name="id" value="{IDENT}"><input name="export" value="download"></form>'.encode()
        with self.assertRaises(ValueError):open_download(Fetch([Response(html,headers={'Content-Type':'text/html'},url='https://drive.usercontent.google.com/download')]),URL)
        self.assertIsNone(file_id('https://evil.test/file/d/'+IDENT+'/view'))

    def test_transfer_validates_drive_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetch=Fetch([Response(PAYLOAD,headers={'Content-Type':'application/zip','Content-Length':str(len(PAYLOAD))},url='https://drive.usercontent.google.com/download')])
            result=a.transfer(fetch,URL,Path(tmp)/'日本語.zip',{'expected_size':len(PAYLOAD)})
            self.assertEqual(result['size'],len(PAYLOAD))
