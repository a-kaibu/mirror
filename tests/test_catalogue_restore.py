import gzip
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
import catalogue_restore as r


class CatalogueRestoreTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.output=self.root/"restored.sqlite3"
        source=self.root/"source.sqlite3";db=sqlite3.connect(source)
        db.execute("CREATE TABLE items(name TEXT)");db.execute("INSERT INTO items VALUES(?)",("new",));db.commit();db.close()
        self.raw=source.read_bytes();self.output.write_bytes(b"previous generation")

    def test_gzip_is_canonical_without_reading_legacy_raw(self):
        response=io.BytesIO(gzip.compress(self.raw));opener=Mock(return_value=response)
        proof=r.restore(self.output,opener=opener,reserve=0)
        self.assertEqual(proof["transport"],"gzip");self.assertEqual(self.output.read_bytes(),self.raw)
        opener.assert_called_once();self.assertTrue(opener.call_args.args[0].full_url.endswith("/index.sqlite3.gz"))
        self.assertTrue(response.closed)

    def test_only_gzip_404_bootstraps_raw_with_the_same_sqlite_validation(self):
        absent=HTTPError("https://source.test/index.sqlite3.gz",404,"missing",{},io.BytesIO())
        response=io.BytesIO(self.raw);opener=Mock(side_effect=[absent,response])
        proof=r.restore(self.output,opener=opener,reserve=0)
        self.assertEqual(proof["transport"],"raw");self.assertEqual(self.output.read_bytes(),self.raw)
        self.assertTrue(opener.call_args.args[0].full_url.endswith("/index.sqlite3"))
        self.assertTrue(response.closed);self.assertTrue(absent.fp.closed)

    def test_present_corrupt_or_truncated_gzip_never_falls_back(self):
        for body in [b"HTML error",gzip.compress(b"not SQLite"),gzip.compress(self.raw)[:-4]]:
            opener=Mock(return_value=io.BytesIO(body))
            with self.assertRaises((OSError,EOFError,ValueError,sqlite3.DatabaseError)):
                r.restore(self.output,opener=opener,reserve=0)
            opener.assert_called_once();self.assertEqual(self.output.read_bytes(),b"previous generation")

    def test_http_and_connection_failures_never_fall_back(self):
        for error in [HTTPError("https://source.test",status,"error",{},io.BytesIO()) for status in [403,429,500,503]]+[TimeoutError()]:
            opener=Mock(side_effect=error)
            with self.assertRaises((HTTPError,TimeoutError)):r.restore(self.output,opener=opener,reserve=0)
            opener.assert_called_once();self.assertEqual(self.output.read_bytes(),b"previous generation")

    def test_invalid_or_oversized_raw_fallback_preserves_previous_generation(self):
        for body in [b"HTML",self.raw]:
            absent=HTTPError("https://source.test",404,"missing",{},io.BytesIO())
            opener=Mock(side_effect=[absent,io.BytesIO(body)])
            with self.assertRaises(ValueError):r.restore(self.output,opener=opener,reserve=0,max_size=len(self.raw)-1)
            self.assertEqual(self.output.read_bytes(),b"previous generation")
