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
            def during_snapshot(store):
                store.db.execute("SELECT count(*) FROM items").fetchone()
                start.set()
                self.assertTrue(done.wait(5),"WAL writer was blocked by export")
                return original(store)
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
