import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import archiver as a
import bulk
from test_archiver import Fetch, Response, HTML, PAYLOAD


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = a.Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, ident, brand="Brand"):
        record = a.parse_detail(HTML, "https://example.test/items/" + ident, "lathercraft")[0]
        record.update(id=ident, brand=brand)
        self.store.add(record)
        return record

    def mark_public(self, ident, path=None):
        sha = hashlib.sha256(PAYLOAD).hexdigest()
        self.store.update(ident, status="verified", path=path, size=len(PAYLOAD), md5=hashlib.md5(PAYLOAD).hexdigest(), sha256=sha,
            remote_sha256=sha, remote_key="vn-archive/mirrors/files/" + sha, remote_verified_at=a.now())

    def test_stream_publish_evict_and_reuse_without_second_download(self):
        self.add("a")
        self.add("b")
        client, objects = Mock(), {}
        client.upload_file.side_effect = lambda path, bucket, key, **kw: objects.update({key: Path(path).read_bytes()})
        with patch.object(a, "s3_client", return_value=client), patch.object(a, "public_digest", side_effect=lambda ns, bucket, key: (hashlib.sha256(objects[key]).hexdigest(), len(objects[key]))):
            stats = bulk.archive_batch(self.store, Fetch([Response(PAYLOAD)]), reserve_bytes=0)
        self.assertEqual(stats["published"], 1)
        self.assertEqual(stats["reused"], 1)
        self.assertEqual(stats["evicted_bytes"], len(PAYLOAD))
        self.assertFalse(list((self.store.root / "files").rglob("*.zip")))
        self.assertTrue(all(row["status"] == "archived" for row in self.store.rows()))
        self.assertEqual(self.store.db.execute("SELECT reused_from FROM items WHERE id='b'").fetchone()[0], "a")
        manifest = self.store.export()
        self.assertTrue(manifest["files"][0]["download_url"])
        self.assertEqual(a.download(self.store, Fetch([])), 0)

    def test_failed_public_readback_keeps_local_file(self):
        self.add("a")
        client = Mock()
        with patch.object(a, "s3_client", return_value=client), patch.object(a, "public_digest", return_value=("wrong", 1)):
            with self.assertRaisesRegex(ValueError, "mismatch"):
                bulk.archive_batch(self.store, Fetch([Response(PAYLOAD)]), reserve_bytes=0)
        row = self.store.rows()[0]
        self.assertIsNone(row["remote_sha256"])
        self.assertEqual((self.store.root / row["path"]).read_bytes(), PAYLOAD)

    def test_eviction_refuses_unverified_or_shared_unpublished_file(self):
        self.add("a")
        self.add("b")
        path = self.store.root / "files" / "managed.zip"
        path.parent.mkdir()
        path.write_bytes(PAYLOAD)
        self.mark_public("a", "files/managed.zip")
        self.store.update("b", status="verified", path="files/managed.zip")
        self.assertEqual(bulk.evict_verified(self.store, "a", "vn-archive", "mirrors"), 0)
        self.assertTrue(path.exists())
        self.assertEqual(bulk.evict_verified(self.store, "b", "vn-archive", "mirrors"), 0)

    def test_eviction_refuses_external_file(self):
        self.add("a")
        self.mark_public("a", "../outside.zip")
        with self.assertRaisesRegex(ValueError, "outside"):
            bulk.evict_verified(self.store, "a", "vn-archive", "mirrors")

    def test_reuse_requires_same_maker_published_digest_and_exact_size(self):
        self.add("a")
        self.mark_public("a")
        self.add("b", "Different maker")
        self.assertFalse(bulk.reuse_verified(self.store, self.store.db.execute("SELECT * FROM items WHERE id='b'").fetchone(), "vn-archive", "mirrors"))
        record = self.add("c")
        record["expected_size"] += 1
        self.store.add(record)
        self.assertFalse(bulk.reuse_verified(self.store, self.store.db.execute("SELECT * FROM items WHERE id='c'").fetchone(), "vn-archive", "mirrors"))

    def test_repeat_publish_does_not_redownload_verified_binary(self):
        self.add("a")
        self.mark_public("a")
        with patch.object(a, "s3_client"), patch.object(a, "public_digest") as read:
            self.assertEqual(a.publish(self.store, "vn-archive", "mirrors", metadata=False), 0)
        read.assert_not_called()

    def test_newly_crawled_small_files_join_running_batch(self):
        a_record=self.add("a");a_record["expected_size"]=10;self.store.add(a_record)
        b_record=self.add("b");b_record["expected_size"]=20;self.store.add(b_record)
        order=[]
        def transfer(store,fetcher,site,ident=None,**kw):
            order.append(ident)
            if ident=="a":
                fresh=self.add("c");fresh["expected_size"]=5;self.store.add(fresh)
            self.store.update(ident,status="failed")
            return 1
        with patch.object(bulk,"download",side_effect=transfer),patch.object(bulk,"publish_metadata"):
            stats=bulk.archive_batch(self.store,Fetch([]),reserve_bytes=0)
        self.assertEqual(order,["a","c","b"])
        self.assertEqual(stats["failed"],3)

    def test_working_primary_is_tried_before_timing_out_secondary(self):
        record = self.add("a")
        record["mirrors"] = ["http://storage2.lathercraft.net/file.zip", "https://storage1.lathercraft.net/file.zip"]
        self.store.add(record)
        fetch = Fetch([Response(PAYLOAD)])
        self.assertEqual(a.download(self.store, fetch), 0)
        self.assertEqual(fetch.requests[0][0], record["mirrors"][1])
        self.assertEqual(json.loads(self.store.rows()[0]["data"])["mirrors"], record["mirrors"])

    def test_missing_lathercraft_link_still_retains_file_metadata(self):
        record = a.parse_detail('<h2>作品 体験版</h2><p><strong>作品:</strong>作品</p><p><strong>ファイル名:</strong>demo.zip</p>', "http://www.lathercraft.net/items/1", "lathercraft")[0]
        self.assertEqual(record["filename"], "demo.zip")
        self.assertEqual(record["mirrors"], [])


if __name__ == "__main__":
    unittest.main()
