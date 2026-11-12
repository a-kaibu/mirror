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

    def test_catalogue_refusal_preserves_capture_query_and_headers(self):
        url='https://web.archive.org/web/20190531095306/http://www.getchu.com/all/demo.html?genre=doujin&page=2'
        headers={'Referer':'https://source.test/'}
        fetch=Fetch([URLError(ConnectionRefusedError(111,'refused')),Response(b'<html>catalogue</html>')])
        with a.open_archive_page(fetch,url,headers) as response:self.assertIn(b'catalogue',response.read())
        self.assertEqual(fetch.requests,[(url,headers),(url.replace('https://','http://',1),headers)])

    def test_catalogue_fallback_is_limited_to_refused_wayback_capture(self):
        import ssl
        url='https://web.archive.org/web/20190531095306/http://www.getchu.com/'
        cases=[(url,HTTPError(url,429,'slow down',{},None)),
               (url,URLError(ssl.SSLCertVerificationError('invalid certificate'))),
               ('https://publisher.test/web/20190531095306/',URLError(ConnectionRefusedError(111,'refused'))),
               ('https://web.archive.org/save',URLError(ConnectionRefusedError(111,'refused')))]
        for target,error in cases:
            with self.subTest(target=target,error=str(error)):
                fetch=Fetch([error])
                with self.assertRaises(type(error)):a.open_archive_page(fetch,target)
                self.assertEqual(len(fetch.requests),1)

    def test_request_encodes_published_spaces_and_unicode_without_double_escaping(self):
        from unittest.mock import Mock
        fetch=a.Fetcher(delay=0,retries=1)
        fetch.opener=Mock()
        url='http://publisher.test/MATERIAL MEL/日本語%20trial.zip?file=体験版+1&sig=a%2Fb'
        fetch.open(url,{'Range':'bytes=10-'})
        request=fetch.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://publisher.test/MATERIAL%20MEL/%E6%97%A5%E6%9C%AC%E8%AA%9E%20trial.zip?file=%E4%BD%93%E9%A8%93%E7%89%88+1&sig=a%2Fb')
        self.assertEqual(request.get_header('Range'),'bytes=10-')
        with self.assertRaises(ValueError):fetch.open('http://publisher.test/file\n.zip')
        self.assertEqual(fetch.opener.open.call_count,1)
