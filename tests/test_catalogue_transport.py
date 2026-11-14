import gzip
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import catalogue_transport as c


class CatalogueTransportTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.source=self.root/"source.sqlite3";self.target=self.root/"restored.sqlite3"
        db=sqlite3.connect(self.source)
        db.execute("CREATE TABLE items(name TEXT)");db.execute("INSERT INTO items VALUES(?)",("日本語体験版",));db.commit();db.close()
        self.raw=self.source.read_bytes();self.body=gzip.compress(self.raw,mtime=0)
        self.target.write_bytes(b"previous good generation")

    def test_deterministic_compression_and_complete_restore(self):
        first=c.compress(self.source,self.root/"first.gz",reserve=0)
        second=c.compress(self.source,self.root/"second.gz",reserve=0)
        self.assertEqual(first,second);self.assertEqual(first["raw_sha256"],hashlib.sha256(self.raw).hexdigest())
        receipt=c.restore(io.BytesIO((self.root/"first.gz").read_bytes()),self.target,reserve=0)
        self.assertEqual(receipt,{"sha256":first["raw_sha256"],"size":first["raw_size"]})
        self.assertEqual(self.target.read_bytes(),self.raw)

    def test_invalid_truncated_and_crc_damaged_streams_preserve_previous_generation(self):
        corrupt=bytearray(self.body);corrupt[-8]^=1
        for body in [b"<html>error</html>",self.body[:-4],bytes(corrupt),gzip.compress(b"not SQLite"),gzip.compress(b"")]:
            with self.subTest(body=body[:12]):
                with self.assertRaises((OSError,EOFError,sqlite3.DatabaseError,ValueError)):
                    c.restore(io.BytesIO(body),self.target,reserve=0)
                self.assertEqual(self.target.read_bytes(),b"previous good generation")
                self.assertEqual(list(self.root.glob(".catalogue-*")),[])

    def test_restore_limit_and_disk_reserve_preserve_previous_generation(self):
        with self.assertRaisesRegex(ValueError,"restore bound"):
            c.restore(io.BytesIO(self.body),self.target,max_size=len(self.raw)-1,reserve=0)
        with patch.object(c.shutil,"disk_usage",return_value=SimpleNamespace(free=0)),self.assertRaises(OSError):
            c.restore(io.BytesIO(self.body),self.target)
        self.assertEqual(self.target.read_bytes(),b"previous good generation")
        self.assertEqual(list(self.root.glob(".catalogue-*")),[])

    def test_compression_disk_failure_does_not_replace_existing_generation(self):
        encoded=self.root/"catalogue.gz";encoded.write_bytes(b"old encoded generation")
        with patch.object(c.shutil,"disk_usage",return_value=SimpleNamespace(free=0)),self.assertRaises(OSError):
            c.compress(self.source,encoded)
        self.assertEqual(encoded.read_bytes(),b"old encoded generation")
        self.assertEqual(list(self.root.glob(".catalogue-*")),[])

    def test_unsafe_destination_and_same_source_are_rejected(self):
        link=self.root/"linked";link.symlink_to(self.target)
        with self.assertRaises(ValueError):c.restore(io.BytesIO(self.body),link)
        with self.assertRaises(ValueError):c.compress(self.source,self.source)
        self.assertEqual(self.source.read_bytes(),self.raw)
