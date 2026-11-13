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

    def test_rounded_size_only_schedules_and_does_not_become_validation_size(self):
        for ident,caption in [('a','2GB'),('z','500KB')]:
            record=self.add(ident)
            record.update(expected_size=None,size_text=caption)
            self.store.add(record)
        client,objects=Mock(),{}
        client.upload_file.side_effect=lambda path,bucket,key,**kw:objects.update({key:Path(path).read_bytes()})
        with patch.object(a,'s3_client',return_value=client),patch.object(a,'public_digest',side_effect=lambda ns,bucket,key:(hashlib.sha256(objects[key]).hexdigest(),len(objects[key]))):
            stats=bulk.archive_batch(self.store,Fetch([Response(PAYLOAD)]),reserve_bytes=0)
        self.assertEqual(stats['published'],1)
        self.assertEqual(self.store.db.execute("SELECT reused_from FROM items WHERE id='a'").fetchone()[0],'z')
        for row in self.store.rows():
            self.assertIsNone(json.loads(row['data'])['expected_size'])
            self.assertEqual(row['size'],len(PAYLOAD))

    def test_busy_catalog_publication_does_not_stop_body_transfer_and_eviction(self):
        from archive_lock import exclusive_file
        self.add("a");self.add("b",brand="Other Brand")
        client,objects=Mock(),{}
        client.upload_file.side_effect=lambda path,bucket,key,**kw:objects.update({key:Path(path).read_bytes()})
        with exclusive_file(self.store.root/".publish.lock"),patch.object(a,"_publish_snapshot") as snapshot,patch.object(a,"s3_client",return_value=client),patch.object(a,"public_digest",side_effect=lambda ns,bucket,key:(hashlib.sha256(objects[key]).hexdigest(),len(objects[key]))):
            stats=bulk.archive_batch(self.store,Fetch([Response(PAYLOAD),Response(PAYLOAD)]),reserve_bytes=0,checkpoint_every=1)
            snapshot.assert_not_called()
        self.assertEqual(stats["processed"],2)
        self.assertEqual(stats["published"],2)
        self.assertTrue(all(row["status"]=="archived" for row in self.store.rows()))
        self.assertFalse(list((self.store.root/"files").rglob("*.zip")))
        with patch.object(a,"_publish_snapshot") as snapshot:
            self.assertTrue(a.publish_metadata(self.store,"vn-archive","mirrors",include_html=False,blocking=False))
            snapshot.assert_called_once()

    def test_nonblocking_checkpoint_still_propagates_upload_errors(self):
        with patch.object(a,"_publish_snapshot",side_effect=BlockingIOError("upload failed")):
            with self.assertRaisesRegex(BlockingIOError,"upload failed"):
                a.publish_metadata(self.store,"vn-archive","mirrors",include_html=False,blocking=False)

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

    def test_reuse_respects_published_sha256_as_well_as_md5(self):
        self.add("a");self.mark_public("a")
        record=self.add("b");record["expected_sha256"]="f"*64;self.store.add(record)
        row=self.store.db.execute("SELECT * FROM items WHERE id='b'").fetchone()
        self.assertFalse(bulk.reuse_verified(self.store,row,"vn-archive","mirrors"))

    def test_quarantined_source_associations_cannot_donate_or_receive_reuse(self):
        for side in ['donor','target']:
            with self.subTest(side=side):
                donor=self.add(side+'-donor',brand=side)
                self.mark_public(donor['id'])
                target=self.add(side+'-target',brand=side)
                flagged=donor if side=='donor' else target
                flagged.setdefault('fields',{})['source_association_quarantined']=True
                self.store.add(flagged)
                row=self.store.db.execute('SELECT * FROM items WHERE id=?',(target['id'],)).fetchone()
                self.assertFalse(bulk.reuse_verified(self.store,row,'vn-archive','mirrors'))
                self.assertIsNone(self.store.db.execute('SELECT remote_verified_at FROM items WHERE id=?',(target['id'],)).fetchone()[0])

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

    def test_three_shards_cover_every_source_exactly_once(self):
        for ident in range(30):self.add(str(ident))
        partitions=[]
        for index in range(3):
            selected=[]
            def fail(store,fetcher,site,ident=None,**kwargs):
                selected.append(ident);store.update(ident,status="failed");return 1
            with patch.object(bulk,"download",side_effect=fail),patch.object(bulk,"publish_metadata"):
                bulk.archive_batch(self.store,Fetch([]),reserve_bytes=0,retry_failed=True,shards=3,shard_index=index)
            partitions.append(set(selected))
        self.assertTrue(all(partitions))
        self.assertEqual(set.union(*partitions),{str(i) for i in range(30)})
        self.assertEqual(sum(map(len,partitions)),30)
        self.assertFalse(partitions[0]&partitions[1] or partitions[1]&partitions[2] or partitions[0]&partitions[2])
        for size,index in [(0,0),(2,-1),(2,2)]:
            with self.assertRaisesRegex(ValueError,"shard"):
                bulk.archive_batch(self.store,Fetch([]),shards=size,shard_index=index)

    def test_overlapping_batches_claim_one_source_only_once(self):
        import threading
        import time
        self.add("shared")
        entered=threading.Event();release=threading.Event();second_ready=threading.Event()
        calls=[];errors=[]
        def transfer(store,fetcher,site,ident=None,**kwargs):
            calls.append(ident);entered.set()
            if not release.wait(5):raise RuntimeError("test release timeout")
            sha=hashlib.sha256(PAYLOAD).hexdigest()
            store.update(ident,status="verified",sha256=sha,md5=hashlib.md5(PAYLOAD).hexdigest(),size=len(PAYLOAD))
            return 0
        def publish(store,namespace,bucket,profile,site,ident=None,**kwargs):
            sha=hashlib.sha256(PAYLOAD).hexdigest()
            store.update(ident,status="archived",path=None,remote_key=f"{namespace}/{bucket}/files/{sha}",remote_sha256=sha,remote_verified_at=a.now())
            return 1
        def worker(second=False):
            store=a.Store(self.store.root)
            try:
                if second:second_ready.set()
                bulk.archive_batch(store,Fetch([]),reserve_bytes=0)
            except BaseException as error:errors.append(error)
            finally:store.db.close()
        with patch.object(bulk,"download",side_effect=transfer),patch.object(bulk,"publish",side_effect=publish),patch.object(bulk,"publish_metadata"):
            first=threading.Thread(target=worker);second=threading.Thread(target=worker,args=(True,))
            first.start();self.assertTrue(entered.wait(3));second.start();self.assertTrue(second_ready.wait(3))
            time.sleep(.1);release.set();first.join(5);second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors,[]);self.assertEqual(calls,["shared"])

    def test_busy_first_source_does_not_block_other_work_and_is_retried(self):
        import threading
        from archive_lock import exclusive_file
        self.add('a');self.add('b')
        claim=self.store.root/'.claims'/(hashlib.sha256(b'a').hexdigest()+'.lock')
        done=threading.Event();result=[];errors=[]
        client,objects=Mock(),{}
        client.upload_file.side_effect=lambda path,bucket,key,**kw:objects.update({key:Path(path).read_bytes()})
        def worker():
            store=a.Store(self.store.root)
            try:result.append(bulk.archive_batch(store,Fetch([Response(PAYLOAD)]),reserve_bytes=0))
            except BaseException as error:errors.append(error)
            finally:store.db.close();done.set()
        with patch.object(a,'s3_client',return_value=client),patch.object(a,'public_digest',side_effect=lambda ns,bucket,key:(hashlib.sha256(objects[key]).hexdigest(),len(objects[key]))):
            with exclusive_file(claim,blocking=False):
                thread=threading.Thread(target=worker);thread.start()
                finished_while_busy=done.wait(3)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished_while_busy,'free source must finish while earlier source stays locked')
        self.assertEqual(errors,[])
        self.assertEqual(result[0]['processed'],1)
        self.assertEqual(result[0]['busy'],1)
        self.assertEqual(self.store.db.execute("SELECT status FROM items WHERE id='a'").fetchone()[0],'pending')
        self.assertEqual(self.store.db.execute("SELECT status FROM items WHERE id='b'").fetchone()[0],'archived')
        with patch.object(bulk,'publish_metadata'):
            resumed=bulk.archive_batch(self.store,Fetch([]),reserve_bytes=0)
        self.assertEqual(resumed['reused'],1)
        self.assertEqual(self.store.db.execute("SELECT status FROM items WHERE id='a'").fetchone()[0],'archived')
    def test_transfer_blocking_io_errors_are_not_mistaken_for_busy_claims(self):
        self.add('a')
        with patch.object(bulk,'download',side_effect=BlockingIOError('body write failed')):
            with self.assertRaisesRegex(BlockingIOError,'body write failed'):
                bulk.archive_batch(self.store,Fetch([]),reserve_bytes=0)

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


class SchedulingSizeTests(unittest.TestCase):
    def test_units_unicode_and_exact_size_precedence(self):
        for caption,value in [('500KB',500*1024),('500KiB',500*1024),('２．５ＧＢ',int(2.5*1024**3)),('1,024 bytes',1024),('900バイト',900)]:
            record={'size_text':caption,'expected_size':None}
            self.assertEqual(bulk.scheduling_size(record,{}),value)
            self.assertIsNone(record['expected_size'])
        record={'expected_size':123,'expected_md5':'hash','size_text':'500KB'}
        self.assertEqual(bulk.scheduling_size(record,{'hash':456}),123)
        record['expected_size']=None
        self.assertEqual(bulk.scheduling_size(record,{'hash':456}),456)

    def test_ambiguous_missing_or_invalid_sizes_stay_unknown(self):
        for caption in [None,{},'不明','1MB + 2MB','0B','-5MB','1.2.3MB','NaN MB','1,2,3MB','9'*400+'GB']:
            self.assertEqual(bulk.scheduling_size({'size_text':caption},{}),2**63)
