import hashlib
import io
import json
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import archiver as a


PAYLOAD = b"PK\x03\x04demo contents"
MD5 = hashlib.md5(PAYLOAD).hexdigest()
HTML = f'''<html><title>Site</title><h1>Demo title</h1><dl>
<dt>ブランド</dt><dd>Brand</dd><dt>ファイルサイズ</dt><dd>{len(PAYLOAD)} bytes</dd>
<dt>MD5</dt><dd>{MD5}</dd></dl>
<a href="https://storage1.test/demo.zip">STORAGE1</a>
<a href="https://storage2.test/demo.zip">STORAGE2</a></html>'''


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None, url="https://www.lathercraft.net/items/1"):
        super().__init__(data)
        self.status, self.url = status, url
        self.headers = Message()
        for k, v in (headers or {}).items():
            self.headers[k] = str(v)

    def geturl(self):
        return self.url


class Fetch:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def open(self, url, headers=None):
        self.requests.append((url, headers))
        r = next(self.responses)
        if isinstance(r, Exception):
            raise r
        return r


class ParserTests(unittest.TestCase):
    def test_mirror_group_and_metadata(self):
        records = a.parse_detail(HTML, "https://www.lathercraft.net/items/1", "lathercraft")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["title"], "Demo title")
        self.assertEqual(r["brand"], "Brand")
        self.assertEqual(r["expected_md5"], MD5)
        self.assertEqual(r["expected_size"], len(PAYLOAD))
        self.assertEqual(len(r["mirrors"]), 2)

    def test_different_files_do_not_share_md5(self):
        html = f'<h1>Product</h1><table><tr><td>MD5 {MD5}</td><td><a href="a.zip">A</a></td></tr><tr><td><a href="b.zip">B</a></td></tr></table>'
        records = a.parse_detail(html, a.HOLY, "holyseal")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["expected_md5"], MD5)
        self.assertIsNone(records[1]["expected_md5"])

    def test_wayback_relative_links(self):
        resolved = a.resolve(a.HOLY, "mlistview.cgi?prdcode=123")
        self.assertEqual(a.original(resolved), "http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=123")
        self.assertTrue(resolved.startswith("https://web.archive.org/web/20181025223313/"))

    def test_wayback_toolbar_link_is_not_followed(self):
        self.assertIsNone(a.resolve(a.HOLY, "https://web.archive.org/save"))

    def test_replayed_link_retains_requested_capture(self):
        link = a.resolve(a.HOLY, "/web/20200101000000/http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=1")
        self.assertIn("/20181025223313/", link)

    def test_legacy_encoding(self):
        html = '<meta charset="Shift_JIS">体験版'
        self.assertEqual(a.decode_html(html.encode("cp932")), html)

    def test_no_download_is_empty(self):
        self.assertEqual(a.parse_detail("<a href='/'>home</a>", "https://example.com/item", "lathercraft"), [])


class DiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "demo.zip"
        self.record = {"expected_md5": MD5, "expected_size": len(PAYLOAD)}

    def tearDown(self):
        self.tmp.cleanup()

    def part(self, url, content, validator='"v1"'):
        suffix = hashlib.sha256(url.encode()).hexdigest()[:16]
        part = self.path.with_name(f"demo.zip.{suffix}.part")
        part.write_bytes(content)
        part.with_suffix(".json").write_text(json.dumps({"validator": validator, "url": url}))
        return part

    def test_full_transfer(self):
        result = a.transfer(Fetch([Response(PAYLOAD, headers={"Content-Length": len(PAYLOAD)})]), "https://mirror.test/a", self.path, self.record)
        self.assertEqual(self.path.read_bytes(), PAYLOAD)
        self.assertEqual(result["md5"], MD5)

    def test_resume_with_validator(self):
        url = "https://mirror.test/a"
        self.part(url, PAYLOAD[:5])
        fetch = Fetch([Response(PAYLOAD[5:], 206, {"ETag": '"v1"', "Content-Range": f"bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}"})])
        a.transfer(fetch, url, self.path, self.record)
        self.assertEqual(fetch.requests[0][1], {"Range": "bytes=5-", "If-Range": '"v1"'})
        self.assertEqual(self.path.read_bytes(), PAYLOAD)

    def test_ignored_range_restarts(self):
        url = "https://mirror.test/a"
        self.part(url, b"old bytes")
        a.transfer(Fetch([Response(PAYLOAD)]), url, self.path, self.record)
        self.assertEqual(self.path.read_bytes(), PAYLOAD)

    def test_partial_without_validator_restarts(self):
        url = "https://mirror.test/a"
        self.part(url, b"old", validator=None)
        fetch = Fetch([Response(PAYLOAD)])
        a.transfer(fetch, url, self.path, self.record)
        self.assertEqual(fetch.requests[0][1], {})

    def test_invalid_range_does_not_publish(self):
        url = "https://mirror.test/a"
        self.part(url, PAYLOAD[:5])
        with self.assertRaisesRegex(ValueError, "Content-Range"):
            a.transfer(Fetch([Response(PAYLOAD, 206, {"Content-Range": "bytes 0-4/5"})]), url, self.path, self.record)
        self.assertFalse(self.path.exists())

    def test_416_restarts(self):
        url = "https://mirror.test/a"
        self.part(url, b"stale part")
        fetch = Fetch([HTTPError(url, 416, "range", {}, None), Response(PAYLOAD)])
        a.transfer(fetch, url, self.path, self.record)
        self.assertIsNone(fetch.requests[1][1])
        self.assertEqual(self.path.read_bytes(), PAYLOAD)

    def test_truncated_body_not_published(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            a.transfer(Fetch([Response(PAYLOAD[:5], headers={"Content-Length": len(PAYLOAD)})]), "https://mirror.test/a", self.path, self.record)
        self.assertFalse(self.path.exists())

    def test_bad_md5_deleted_for_retry(self):
        url = "https://mirror.test/a"
        with self.assertRaisesRegex(ValueError, "MD5"):
            a.transfer(Fetch([Response(b"bad payload")]), url, self.path, self.record)
        self.assertFalse(list(self.root.glob("*.part")))

    def test_html_error_without_content_type(self):
        with self.assertRaisesRegex(ValueError, "HTML"):
            a.transfer(Fetch([Response(b"<!doctype html><body>404</body>")]), "https://mirror.test/a", self.path, {})

    def test_same_url_new_etag_cannot_append(self):
        url = "https://mirror.test/a"
        self.part(url, PAYLOAD[:5])
        with self.assertRaisesRegex(ValueError, "validator changed"):
            a.transfer(Fetch([Response(PAYLOAD[5:], 206, {"Content-Range": f"bytes 5-{len(PAYLOAD)-1}/{len(PAYLOAD)}", "ETag": '"v2"'})]), url, self.path, self.record)
        self.assertFalse(self.path.exists())

    def test_mirror_failover_and_manifest(self):
        store = a.Store(self.root)
        record = a.parse_detail(HTML, "https://www.lathercraft.net/items/1", "lathercraft")[0]
        store.add(record)
        self.assertEqual(a.download(store, Fetch([URLError("offline"), Response(PAYLOAD)])), 0)
        row = store.rows()[0]
        self.assertEqual(row["status"], "verified")
        manifest = json.loads((self.root / "manifest.json").read_text())
        source = manifest["files"][0]["sources"][0]
        self.assertEqual(len(source["attempts"]), 2)
        self.assertEqual(source["source_url"], "https://storage2.test/demo.zip")
        self.assertEqual(a.verify(store), 0)
        (self.root / row["path"]).write_bytes(b"corrupt")
        self.assertEqual(a.verify(store), 1)
        self.assertEqual(store.rows()[0]["status"], "failed")
        store.db.close()

    def test_bounded_crawl_and_repeat(self):
        store = a.Store(self.root)
        fetch = Fetch([Response(b'<a href="/items/1">one</a><a href="/items/2">two</a>'), Response(HTML.encode())])
        self.assertEqual(a.crawl(store, fetch, "lathercraft", "https://www.lathercraft.net/items?page=1", max_pages=1, max_items=1), 0)
        self.assertEqual(len(store.rows()), 1)
        self.assertEqual(len(fetch.requests), 2)
        store.db.close()

    def test_unrecognized_page_fails(self):
        store = a.Store(self.root)
        self.assertEqual(a.crawl(store, Fetch([Response(b"<html>challenge</html>")]), "lathercraft", "https://www.lathercraft.net/items?page=1"), 1)
        self.assertEqual(store.db.execute("SELECT status FROM pages").fetchone()[0], "failed")
        store.db.close()


class PublishTests(unittest.TestCase):
    def test_public_roundtrip(self):
        from botocore.exceptions import ClientError
        client, public = Mock(), Mock()
        client.head_bucket.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadBucket")
        objects = {}
        def upload(path, bucket, key, **kwargs):
            objects[key] = Path(path).read_bytes()
        client.upload_file.side_effect = upload
        public.get_object.side_effect = lambda Bucket, Key: {"Body": io.BytesIO(objects[Key])}
        with tempfile.TemporaryDirectory() as tmp:
            store = a.Store(tmp)
            record = a.parse_detail(HTML, "https://www.lathercraft.net/items/1", "lathercraft")[0]
            store.add(record)
            a.download(store, Fetch([Response(PAYLOAD)]))
            with patch.object(a, "s3_client", return_value=client), patch.object(a, "public_digest", side_effect=lambda namespace, bucket, key: (hashlib.sha256(objects[key]).hexdigest(), len(objects[key]))):
                a.publish(store, "vn-archive", "mirrors")
            client.create_bucket.assert_called_once_with(Bucket="mirrors")
            self.assertEqual(store.rows()[0]["remote_sha256"], hashlib.sha256(PAYLOAD).hexdigest())
            self.assertIn("manifest.json", objects)
            self.assertIn("files/" + hashlib.sha256(PAYLOAD).hexdigest(), objects)
            store.db.close()

    def test_init_public_write_read(self):
        client, public = Mock(), Mock()
        captured = {}
        client.put_object.side_effect = lambda **kwargs: captured.update(kwargs)
        public.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(captured["Body"])}
        with patch.object(a, "s3_client", return_value=client), patch.object(a, "public_digest", side_effect=lambda *args: (hashlib.sha256(captured["Body"]).hexdigest(), len(captured["Body"]))):
            a.init_bucket("vn-archive", "mirrors")
        client.create_bucket.assert_called_once_with(Bucket="mirrors")
        self.assertEqual(captured["Key"], "ABOUT.txt")

    def test_remote_mismatch_not_success(self):
        client, public = Mock(), Mock()
        public.get_object.return_value = {"Body": io.BytesIO(b"corrupt")}
        with patch.object(a, "s3_client", return_value=client), patch.object(a, "public_digest", return_value=("bad", 7)):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                a.init_bucket("vn-archive", "mirrors")

    def test_s3_gateway_configuration(self):
        import boto3
        with patch.object(boto3, "Session") as session:
            a.s3_client("vn-archive")
        session.assert_called_once_with(profile_name="hf")
        kwargs = session.return_value.client.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], "https://vn-archive.s3.hf.co")
        self.assertEqual(kwargs["config"].s3["addressing_style"], "path")
        self.assertEqual(kwargs["config"].request_checksum_calculation, "when_required")


if __name__ == "__main__":
    unittest.main()
