import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import archiver as a
import catalog
from test_archiver import HTML, PAYLOAD, Fetch, Response


class ConcurrentCollectionTests(unittest.TestCase):
    def record(self, ident):
        r=a.parse_detail(HTML,"https://example.test/items/"+ident,"lathercraft")[0]
        r["id"]=ident
        return r

    def test_crawler_commits_during_consistent_export_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            reader=a.Store(tmp);reader.add(self.record("a"))
            start=threading.Event();done=threading.Event();errors=[]
            def produce():
                writer=a.Store(tmp)
                try:
                    if not start.wait(5):raise AssertionError("snapshot never started")
                    writer.add(self.record("b"))
                except Exception as e:errors.append(e)
                finally:writer.db.close();done.set()
            thread=threading.Thread(target=produce);thread.start()
            original=catalog.manifest
            def during_snapshot(store,**options):
                store.db.execute("SELECT count(*) FROM items").fetchone()
                start.set()
                self.assertTrue(done.wait(5),"WAL writer was blocked by export")
                return original(store,**options)
            with patch.object(catalog,"manifest",side_effect=during_snapshot):data=reader.export()
            thread.join(5)
            self.assertFalse(errors,errors)
            self.assertEqual(len(data["files"]),1)
            self.assertEqual(len(reader.export()["files"]),2)
            reader.db.close()

    def test_transfer_cannot_overwrite_new_source_expectations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);record=self.record("a");store.add(record)
            class ChangingResponse(Response):
                changed=False
                def read(inner,*args):
                    if not inner.changed:
                        inner.changed=True
                        newer=dict(record,expected_md5="f"*32)
                        store.add(newer)
                    return super().read(*args)
            a.download(store,Fetch([ChangingResponse(PAYLOAD),OSError("next mirror offline")]))
            row=store.rows()[0]
            self.assertEqual(row["status"],"pending")
            self.assertEqual(json.loads(row["data"])["expected_md5"],"f"*32)
            self.assertIsNone(row["remote_sha256"])
            store.db.close()

    def test_matching_current_checksum_accepts_descriptive_changes_during_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);record=self.record('a');store.add(record)
            class ChangingResponse(Response):
                changed=False
                def read(inner,*args):
                    if not inner.changed:
                        inner.changed=True
                        store.add(dict(record,page_url='https://example.test/new-capture',fields={'new_source_note':'same build'}))
                    return super().read(*args)
            fetch=Fetch([ChangingResponse(PAYLOAD)])
            try:
                self.assertEqual(a.download(store,fetch,export=False),0)
                row=store.rows()[0]
                self.assertEqual(row['status'],'verified')
                self.assertEqual(json.loads(row['data'])['fields']['new_source_note'],'same build')
                self.assertEqual(len(fetch.requests),1)
            finally:store.db.close()

    def test_completed_transfer_survives_rejection_and_resumes_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);record=self.record('a');store.add(record)
            class ChangingResponse(Response):
                changed=False
                def read(inner,*args):
                    if not inner.changed:
                        inner.changed=True;store.add(dict(record,expected_md5='f'*32))
                    return super().read(*args)
            try:
                self.assertEqual(a.download(store,Fetch([ChangingResponse(PAYLOAD),OSError('offline')]),export=False),1)
                receipt=store.db.execute("select url,attempted_at from attempts where error like 'source metadata changed during transfer%' order by id desc limit 1").fetchone()
                store.add(record);never=Mock()
                self.assertEqual(a.download(store,never,export=False),0);never.open.assert_not_called()
                row=store.rows()[0]
                self.assertEqual(row['status'],'verified')
                self.assertEqual(row['source_url'],receipt['url'])
                self.assertEqual(row['fetched_at'],receipt['attempted_at'])
                self.assertEqual(row['sha256'],hashlib.sha256(PAYLOAD).hexdigest())
            finally:store.db.close()

    def test_completed_recovery_rejects_unproved_or_corrupted_working_copies(self):
        for mode in ['no_receipt','no_checksum','corrupt']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                store=a.Store(tmp);record=self.record('a')
                if mode=='no_checksum':record['expected_md5']=None
                store.add(record)
                path=Path(tmp)/'files/lathercraft/a/demo.zip';path.parent.mkdir(parents=True);path.write_bytes(b'bad' if mode=='corrupt' else PAYLOAD)
                if mode!='no_receipt':
                    store.db.execute('insert into attempts(item_id,url,attempted_at,error) values(?,?,?,?)',('a','https://old.test/demo.zip',a.now(),'source metadata changed during transfer; retry against current expectations'));store.db.commit()
                fetch=Fetch([Response(PAYLOAD)])
                try:
                    self.assertEqual(a.download(store,fetch,export=False),0)
                    self.assertEqual(len(fetch.requests),1)
                    self.assertNotEqual(store.rows()[0]['source_url'],'https://old.test/demo.zip')
                finally:store.db.close()

    def test_publish_cannot_acknowledge_changed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);record=self.record("a");store.add(record)
            a.download(store,Fetch([Response(PAYLOAD)]))
            def changed(*args):
                store.add(dict(record,expected_md5="f"*32))
                return hashlib.sha256(PAYLOAD).hexdigest(),len(PAYLOAD)
            with patch.object(a,"s3_client",return_value=Mock()),patch.object(a,"public_digest",side_effect=changed):
                with self.assertRaisesRegex(ValueError,"metadata changed"):
                    a.publish(store,"vn-archive","mirrors",metadata=False)
            row=store.rows()[0]
            self.assertEqual(row["status"],"pending")
            self.assertIsNone(row["remote_sha256"])
            self.assertTrue((store.root/row["path"]).exists())
            store.db.close()

    def test_parallel_metadata_bounds_requests_and_stages_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);store.add(self.record("a"))
            (store.root/"html").mkdir()
            for i in range(7):
                path=f"html/{i}.html";(store.root/path).write_text(f"source {i}")
                store.db.execute("INSERT INTO pages(url,path,status) VALUES(?,?,?)",(f"https://source.test/{i}",path,"ok"))
            store.db.commit()
            client=Mock();objects={};lock=threading.Lock();barrier=threading.Barrier(4)
            state={"active":0,"peak":0,"arrivals":0}
            def upload(path,bucket,key,**kwargs):
                if key=="index.sqlite3":
                    objects[key]=Path(path).read_bytes();return
                with lock:
                    index=state["arrivals"];state["arrivals"]+=1
                    state["active"]+=1;state["peak"]=max(state["peak"],state["active"])
                try:
                    if index<4:barrier.wait(5)
                    if key=="html/0.html":(store.root/"html/0.html").write_text("concurrent replacement")
                    objects[key]=Path(path).read_bytes()
                finally:
                    with lock:state["active"]-=1
            client.upload_file.side_effect=upload
            from test_snapshot_upload import bind_snapshot_gateway
            bind_snapshot_gateway(client,objects)
            with patch.object(a,"s3_client",return_value=client),patch.object(a,"public_digest",side_effect=lambda ns,bucket,key:(hashlib.sha256(objects[key]).hexdigest(),len(objects[key]))):
                a.publish_metadata(store,"vn-archive","mirrors")
            self.assertEqual(state["peak"],4)
            self.assertNotIn("manifest.json",objects)
            self.assertEqual(objects["html/0.html"],b"source 0")
            self.assertEqual(store.db.execute("SELECT count(*) FROM published_objects").fetchone()[0],7)
            store.db.close()


class MetadataCheckpointIsolationTests(unittest.TestCase):
    def test_catalog_checkpoint_does_not_wait_for_html_upload(self):
        from types import SimpleNamespace
        entered=threading.Event();release=threading.Event();errors=[]
        def slow_html(*args):entered.set();release.wait(5)
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'_publish_html',side_effect=slow_html),patch.object(a,'_publish_snapshot') as snapshot:
            store=SimpleNamespace(root=Path(tmp))
            def background():
                try:a.publish_metadata(store,'vn-archive','mirrors')
                except BaseException as error:errors.append(error)
            thread=threading.Thread(target=background);thread.start()
            try:
                self.assertTrue(entered.wait(2))
                a.publish_metadata(store,'vn-archive','mirrors',include_html=False)
                self.assertEqual(snapshot.call_count,1)
                self.assertTrue(thread.is_alive())
            finally:release.set();thread.join(5)
            self.assertEqual(errors,[])
            self.assertEqual(snapshot.call_count,2)


    def test_verified_gzip_checkpoint_is_independent_of_legacy_raw_lock(self):
        from types import SimpleNamespace
        from archive_lock import exclusive_file
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'_publish_snapshot') as snapshot:
            store=SimpleNamespace(root=Path(tmp))
            with exclusive_file(store.root/'.publish.lock'):
                self.assertTrue(a.publish_metadata(store,'vn-archive','mirrors',include_html=False,blocking=False))
                snapshot.assert_called_once()
                with exclusive_file(store.root/'.publish-gzip.lock'):
                    self.assertFalse(a.publish_metadata(store,'vn-archive','mirrors',include_html=False,blocking=False))
                snapshot.assert_called_once()
