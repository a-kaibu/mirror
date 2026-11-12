import tempfile
import unittest
from urllib.error import URLError,HTTPError
import archiver as a
from test_archiver import Fetch,Response,HTML,PAYLOAD

class WaybackTransportTests(unittest.TestCase):
    def test_refused_https_capture_retries_same_http_capture_and_keeps_audit(self):
        secure="https://web.archive.org/web/20190102030405id_/http://mirror.test/demo.zip?ver=1"
        plain="http://web.archive.org/web/20190102030405id_/http://mirror.test/demo.zip?ver=1"
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            record=a.parse_detail(HTML,"https://source.test/item/1","lathercraft")[0]
            record["mirrors"]=[secure];store.add(record)
            fetch=Fetch([URLError(ConnectionRefusedError(111,"refused")),Response(PAYLOAD)])
            self.assertEqual(a.download(store,fetch,export=False),0)
            self.assertEqual([q[0] for q in fetch.requests],[secure,plain])
            row=store.rows()[0]
            self.assertEqual(row["source_url"],plain)
            self.assertEqual(row["status"],"verified")
            attempts=store.db.execute("SELECT url,error FROM attempts ORDER BY id").fetchall()
            self.assertIsNotNone(attempts[0][1]);self.assertIsNone(attempts[1][1])
            store.db.close()

    def test_no_downgrade_for_unknown_bytes_certificate_errors_or_http_denial(self):
        import ssl
        for checksum,error in [(False,URLError(ConnectionRefusedError(111,"refused"))),
                               (True,URLError(ssl.SSLCertVerificationError("invalid certificate"))),
                               (True,HTTPError("https://web.archive.org/",403,"forbidden",{},None))]:
            with self.subTest(checksum=checksum,error=type(error).__name__),tempfile.TemporaryDirectory() as tmp:
                store=a.Store(tmp)
                record=a.parse_detail(HTML,"https://source.test/item/1","lathercraft")[0]
                if not checksum:record["expected_md5"]=None
                record["mirrors"]=["https://web.archive.org/web/20190102030405id_/http://mirror.test/demo.zip"]
                store.add(record);fetch=Fetch([error])
                self.assertEqual(a.download(store,fetch,export=False),1)
                self.assertEqual(len(fetch.requests),1)
                store.db.close()

    def test_http_capture_still_must_match_publisher_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            record=a.parse_detail(HTML,"https://source.test/item/1","lathercraft")[0]
            record["mirrors"]=["https://web.archive.org/web/20190102030405id_/http://mirror.test/demo.zip"]
            store.add(record)
            fetch=Fetch([URLError(ConnectionRefusedError(111,"refused")),Response(b"wrong data")])
            self.assertEqual(a.download(store,fetch,export=False),1)
            self.assertEqual(store.rows()[0]["status"],"failed")
            store.db.close()

    def test_cdx_connection_fallback_does_not_hide_access_denial(self):
        url="https://web.archive.org/cdx/search/cdx?url=http%3A%2F%2Fmirror.test%2Fdemo.zip"
        fetch=Fetch([URLError(ConnectionRefusedError(111,"refused")),Response(b"[]")])
        with a.open_archive_index(fetch,url) as response:self.assertEqual(response.read(),b"[]")
        self.assertEqual(fetch.requests[1][0],url.replace("https://","http://",1))
        denied=Fetch([HTTPError(url,403,"forbidden",{},None)])
        with self.assertRaises(HTTPError):a.open_archive_index(denied,url)
        self.assertEqual(len(denied.requests),1)
        with self.assertRaises(ValueError):a.open_archive_index(Fetch([]),"https://other.test/cdx/search/cdx")
