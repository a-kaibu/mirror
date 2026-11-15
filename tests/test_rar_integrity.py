import binascii
import ctypes.util
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import rar_integrity as rar


def block(kind,flags,body=b''):
    payload=struct.pack('<BHH',kind,flags,7+len(body))+body
    return struct.pack('<H',binascii.crc32(payload)&0xffff)+payload


def stored_rar(payload=b'original disk image',*,flags=0x8000,main_flags=0,unpacked=None):
    name=b'disk.img'
    body=struct.pack('<IIBIIBBHI',len(payload),len(payload) if unpacked is None else unpacked,
        3,binascii.crc32(payload),0,20,0x30,len(name),0o100644)+name
    return b'Rar!\x1a\x07\x00'+block(0x73,main_flags,b'\0'*6)+block(0x74,flags,body)+payload+block(0x7b,0x4000)


class RarHeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'game.rar'

    def test_headers_retain_member_size_and_crc(self):
        payload=b'original disk image';self.path.write_bytes(stored_rar(payload))
        self.assertEqual(rar.member_headers(self.path),[{'size':len(payload),'crc':binascii.crc32(payload)}])

    def test_damaged_headers_truncation_and_trailing_data_fail(self):
        original=stored_rar();damaged=bytearray(original);damaged[8]^=1
        for data in (bytes(damaged),original[:len(original)//2],original[:-7],original+b'x',b'Rar!\x1a\x07\x01\0'):
            with self.subTest(data=data[:12]):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):rar.member_headers(self.path)

    def test_split_encrypted_and_multivolume_archives_fail(self):
        for data in (stored_rar(flags=0x8001),stored_rar(flags=0x8002),stored_rar(flags=0x8004),stored_rar(main_flags=1),stored_rar(main_flags=0x80)):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):rar.member_headers(self.path)

    def test_expansion_and_compressed_bounds_precede_native_decoding(self):
        self.path.write_bytes(stored_rar(unpacked=rar.MAX_EXPANDED+1))
        with self.assertRaisesRegex(ValueError,'expanded size'):rar.member_headers(self.path)
        self.path.write_bytes(stored_rar())
        with patch.object(rar,'MAX_FILE',1),self.assertRaisesRegex(ValueError,'input size'):rar.member_headers(self.path)

    def test_missing_native_library_fails_without_approving(self):
        self.path.write_bytes(stored_rar())
        with patch.object(rar.ctypes.util,'find_library',return_value=None),self.assertRaisesRegex(ValueError,'requires libarchive'):
            rar._verify(self.path)


class RarDecoderTests(unittest.TestCase):
    setUp=RarHeaderTests.setUp

    @unittest.skipUnless(os.name=='posix' and ctypes.util.find_library('archive'),'optional POSIX libarchive review')
    def test_independent_crc_accepts_valid_and_rejects_changed_data(self):
        original=stored_rar();self.path.write_bytes(original)
        result=rar.verify(self.path)
        self.assertEqual(result['sha256'],hashlib.sha256(original).hexdigest())
        self.assertEqual((result['members'],result['rar_crc'],result['extracted'],result['executed']),(1,'ok',False,False))
        corrupt=original.replace(b'original disk image',b'Original disk image');self.path.write_bytes(corrupt)
        with self.assertRaisesRegex(ValueError,'isolated validation failed'):rar.verify(self.path)


class RarReviewTests(unittest.TestCase):
    def setUp(self):
        from test_size_reviews import SizeReviewTests
        SizeReviewTests.setUp(self)
        self.payload=stored_rar()
        self.current.write_bytes(self.payload);self.historical.write_bytes(self.payload)
        target='http://retropc.net/alice/files/GAME.RAR'
        self.record={**self.record,'filename':'GAME.RAR','mirrors':[target],'expected_size':len(self.payload)+7}
        self.store.add(self.record)
        import base64
        self.capture={**self.capture,'url':'http://web.archive.org/web/20101128212158id_/'+target,
            'original_url':target,'index_digest':base64.b32encode(hashlib.sha1(self.payload).digest()).decode()}

    @unittest.skipUnless(os.name=='posix' and ctypes.util.find_library('archive'),'optional POSIX libarchive review')
    def test_rar_review_preserves_publisher_claim_and_survives_recrawl(self):
        from size_reviews import review
        import mirror_sources as m
        ident=review(self.store,self.record,self.current,self.historical,self.capture)
        row=self.store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone();data=json.loads(row['data'])
        self.assertEqual(row['status'],'pending');self.assertIsNone(row['remote_verified_at'])
        self.assertEqual(data['expected_size'],len(self.payload));self.assertEqual(data['fields']['publisher_expected_size'],len(self.payload)+7)
        self.assertIn('RAR CRC',data['fields']['size_review']['basis'])
        self.assertEqual(data['fields']['size_review']['evidence']['rar_crc'],'ok')
        self.assertEqual(m.add_record(self.store,dict(self.record)),ident)

    def test_wrong_validator_identity_cannot_create_a_review(self):
        from size_reviews import review
        with patch.object(rar,'verify',return_value={'sha256':'f'*64,'size':len(self.payload)}):
            with self.assertRaisesRegex(ValueError,'content identity changed'):
                review(self.store,self.record,self.current,self.historical,self.capture)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],0)

    @unittest.skipUnless(os.name=='posix' and ctypes.util.find_library('archive'),'optional POSIX libarchive review')
    def test_matching_corrupt_copies_and_cdx_cannot_waive_crc(self):
        import base64
        from size_reviews import review
        corrupt=self.payload.replace(b'original disk image',b'Original disk image')
        self.current.write_bytes(corrupt);self.historical.write_bytes(corrupt)
        self.capture['index_digest']=base64.b32encode(hashlib.sha1(corrupt).digest()).decode()
        with self.assertRaisesRegex(ValueError,'isolated validation failed'):
            review(self.store,self.record,self.current,self.historical,self.capture)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],0)
