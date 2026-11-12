import hashlib
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

import archiver as a
from test_archiver import HTML, PAYLOAD, Fetch, Response


class SourceTLSTests(unittest.TestCase):
    def test_exact_leaf_pin_checked_after_handshake(self):
        der = b"source certificate"
        callback = Mock()
        connection = a.PinnedHTTPSConnection("storage1.lathercraft.net", fingerprint=hashlib.sha256(der).hexdigest(), on_verified=callback)
        connection.sock = Mock()
        connection.sock.getpeercert.return_value = der
        with patch.object(a.http.client.HTTPSConnection, "connect"):
            connection.connect()
        connection.sock.getpeercert.assert_called_once_with(binary_form=True)
        callback.assert_called_once_with(hashlib.sha256(der).hexdigest())
        connection.close()

    def test_changed_certificate_fails_closed(self):
        callback = Mock()
        connection = a.PinnedHTTPSConnection("storage1.lathercraft.net", fingerprint="0" * 64, on_verified=callback)
        connection.sock = Mock()
        connection.sock.getpeercert.return_value = b"replacement certificate"
        with patch.object(a.http.client.HTTPSConnection, "connect"), patch.object(connection, "close") as close:
            with self.assertRaisesRegex(ssl.SSLCertVerificationError, "pin mismatch"):
                connection.connect()
            close.assert_called_once()
        callback.assert_not_called()

    def test_hf_and_redirects_to_other_hosts_use_normal_tls(self):
        handler = a.SourceHTTPSHandler({"storage1.lathercraft.net": "a" * 64}, Mock())
        def construct(factory, request, **kw):
            return factory(request.host, **kw)
        with patch.object(handler, "do_open", side_effect=construct):
            source = handler.https_open(Request("https://storage1.lathercraft.net/file.zip"))
            self.assertIsInstance(source, a.PinnedHTTPSConnection)
            source.close()
            for url in ["https://huggingface.co/file", "https://api.github.com/file", "https://other.example/file"]:
                connection = handler.https_open(Request(url))
                self.assertNotIsInstance(connection, a.PinnedHTTPSConnection)
                self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(connection._context.check_hostname)
                connection.close()

    def test_pin_selected_for_proxy_tunnel_target(self):
        handler = a.SourceHTTPSHandler({"storage1.lathercraft.net": "a" * 64}, Mock())
        request = Request("https://storage1.lathercraft.net/file.zip")
        request._tunnel_host = "storage1.lathercraft.net:443"
        with patch.object(handler, "do_open", side_effect=lambda factory, req, **kw: factory("proxy.example:8080", **kw)):
            connection = handler.https_open(request)
        self.assertIsInstance(connection, a.PinnedHTTPSConnection)
        connection.close()

    def test_missing_publisher_md5_is_retained_without_blocking_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = a.Store(tmp)
            record = a.parse_detail(HTML, "https://example.test/items/1", "lathercraft")[0]
            record["expected_md5"] = None
            store.add(record)
            fetch = Fetch([Response(PAYLOAD)])
            fetch.tls_pins = {"storage1.lathercraft.net": "a" * 64}
            fetch.last_tls_pin = "a" * 64
            self.assertEqual(a.download(store, fetch), 0)
            row = store.rows()[0]
            self.assertIsNone(__import__("json").loads(row["data"])["expected_md5"])
            self.assertEqual(row["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
            self.assertEqual(store.db.execute("SELECT tls_pin FROM attempts").fetchone()[0], "a" * 64)
            store.db.close()


if __name__ == "__main__":
    unittest.main()
