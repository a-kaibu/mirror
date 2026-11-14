from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import archiver as a
from archive_lock import exclusive_file
import multipart_upload as m
import snapshot_upload as s
from test_archiver import HTML
from test_multipart_upload import Gateway


class VersionedGateway(Gateway):
    def __init__(self):super().__init__();self.metadata={};self.omit_metadata=False
    def create_multipart_upload(self,**kw):
        result=super().create_multipart_upload(**kw);self.metadata[result['UploadId']]=kw['Metadata'];return result
    def complete_multipart_upload(self,**kw):
        self.metadata[kw['Key']]=self.metadata[kw['UploadId']]
        return super().complete_multipart_upload(**kw)
    def head_object(self,**kw):
        result=super().head_object(**kw)
        result['ETag']=hashlib.md5(self.objects[kw['Key']]).hexdigest()
        if not self.omit_metadata:result['Metadata']=self.metadata.get(kw['Key'],{})
        return result


def bind_snapshot_gateway(client,objects):
    """Give a small-file mock the same independently checked multipart server."""
    gateway=VersionedGateway();gateway.objects=objects
    for method in ['head_object','create_multipart_upload','upload_part','list_parts','complete_multipart_upload','abort_multipart_upload']:
        getattr(client,method).side_effect=getattr(gateway,method)
    return gateway


class SnapshotUploadTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://source.test/items/a','lathercraft')[0];self.record['id']='a';self.store.add(self.record)
        self.client=VersionedGateway();self.key='index.sqlite3'
    def publish(self,readback=None):
        def proof(*args):
            self.assertFalse(self.store.db.in_transaction)
            body=self.client.objects[self.key];return hashlib.sha256(body).hexdigest(),len(body)
        with patch.object(a,'public_digest',side_effect=readback or proof):
            s.publish(self.store,self.client,'vn-archive','mirrors')
    def pointer(self):return s.slot(self.store.root,'vn-archive','mirrors')/'current.json'
    def values(self):
        path=self.store.root/'readback.sqlite3';path.write_bytes(self.client.objects[self.key])
        with closing(sqlite3.connect(path)) as db:return [row[0] for row in db.execute('SELECT id FROM items ORDER BY id')]
    def large(self):
        self.store.db.execute('CREATE TABLE payload(value BLOB)');self.store.db.execute('INSERT INTO payload VALUES(zeroblob(?))',(9*1024**2,));self.store.db.commit()
    def add_second(self):self.store.add(dict(self.record,id='b'))

    def test_accepted_parts_resume_same_snapshot_while_source_changes(self):
        self.large();self.client.fail_part=2
        with self.assertRaises(TimeoutError):self.publish()
        first=json.loads(self.pointer().read_text());self.assertEqual(self.client.sent,[1,2]);self.add_second()
        with patch.object(a,'_compact_snapshot',side_effect=AssertionError('resume rebuilt the snapshot')):self.publish()
        self.assertEqual(self.client.sent,[1,2,2]);self.assertEqual(self.values(),['a']);self.assertFalse(self.pointer().exists())
        self.assertFalse(m.snapshot_path(self.store.root,'vn-archive','mirrors',first['sha256']).exists())
        self.publish();self.assertEqual(self.values(),['a','b'])

    def test_completed_readback_failure_keeps_stage_and_never_resends(self):
        with self.assertRaises(TimeoutError):self.publish(lambda *args:(_ for _ in ()).throw(TimeoutError()))
        self.assertTrue(self.pointer().exists());sent=list(self.client.sent)
        self.publish();self.assertEqual(self.client.sent,sent);self.assertFalse(self.pointer().exists())

    def test_lost_completion_without_metadata_uses_full_readback(self):
        self.client.fail_completion=True;self.client.omit_metadata=True
        with self.assertRaises(TimeoutError):self.publish()
        sent=list(self.client.sent);self.publish();self.assertEqual(self.client.sent,sent)
        receipt=s.slot(self.store.root,'vn-archive','mirrors')/'last-verified.json'
        self.assertTrue(json.loads(receipt.read_text())['verified_at'])

    def test_external_generation_retires_old_stage_and_captures_new_metadata(self):
        self.client.fail_part=1
        with self.assertRaises(TimeoutError):self.publish()
        old=json.loads(self.pointer().read_text());self.add_second()
        self.client.objects[self.key]=b'external replacement';self.client.metadata[self.key]={'sha256':'0'*64}
        self.publish();self.assertEqual(self.values(),['a','b']);self.assertEqual(self.client.aborts,0)
        self.assertFalse(m.snapshot_path(self.store.root,'vn-archive','mirrors',old['sha256']).exists())

    def test_public_mismatch_keeps_stage_and_does_not_write_verified_receipt(self):
        with self.assertRaises(m.PublicReadbackMismatch):self.publish(lambda *args:('0'*64,1))
        self.assertTrue(self.pointer().exists())
        self.assertFalse((self.pointer().parent/'last-verified.json').exists())

    def test_disk_guard_blocks_creation_but_not_an_existing_resume(self):
        with patch.object(s.shutil,'disk_usage',return_value=SimpleNamespace(free=0)),self.assertRaises(OSError):self.publish()
        self.assertEqual(self.client.created,0);self.assertFalse(self.pointer().exists())
        self.client.fail_part=1
        with self.assertRaises(TimeoutError):self.publish()
        with patch.object(s.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):self.publish()
        self.assertEqual(self.values(),['a'])

    def test_busy_destination_does_not_start_upload(self):
        identity=hashlib.sha256(m.json_identity('vn-archive','mirrors').encode()).hexdigest()
        with exclusive_file(self.store.root/'.claims'/('snapshot-'+identity+'.lock')),self.assertRaises(BlockingIOError):self.publish()
        self.assertEqual(self.client.created,0)

    def test_canonical_exception_rejects_arbitrary_local_paths(self):
        path=self.store.root/'index.sqlite3'
        with self.assertRaisesRegex(ValueError,'immutable managed staging'):
            m.transfer_snapshot_and_readback(self.store,self.client,'vn-archive','mirrors',path,'0'*64,path.stat().st_size,lambda:None)
        self.assertEqual(self.client.created,0)

    def test_pending_destination_identity_is_checked_before_remote_requests(self):
        self.client.fail_part=1
        with self.assertRaises(TimeoutError):self.publish()
        state=json.loads(self.pointer().read_text());state['bucket']='other';self.pointer().write_text(json.dumps(state))
        with patch.object(self.client,'head_object',side_effect=AssertionError('unvalidated state reached network')):
            with self.assertRaisesRegex(ValueError,'identity changed'):self.publish()

    def test_orphan_backup_is_removed_without_touching_unmanaged_files(self):
        directory=s.slot(self.store.root,'vn-archive','mirrors')
        old=directory/('0'*64);old.mkdir(parents=True);(old/'index.sqlite3').write_bytes(b'unreferenced derivative')
        untouched=directory/'keep.txt';untouched.write_text('unmanaged')
        self.publish();self.assertFalse(old.exists());self.assertEqual(untouched.read_text(),'unmanaged')

    def test_removed_canonical_object_rebuilds_without_fetching_missing_bytes(self):
        self.client.objects[self.key]=b'previous generation';self.client.fail_part=1
        with self.assertRaises(TimeoutError):self.publish()
        del self.client.objects[self.key];self.add_second()
        self.publish();self.assertEqual(self.values(),['a','b'])

    def test_symlinked_cache_directory_is_rejected_before_writes(self):
        directory=s.slot(self.store.root,'vn-archive','mirrors');directory.parent.mkdir(parents=True)
        outside=self.store.root/'other';outside.mkdir();directory.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'unsafe snapshot cache'):self.publish()
        self.assertEqual(list(outside.iterdir()),[]);self.assertEqual(self.client.created,0)

    def test_snapshot_does_not_materialize_or_write_the_full_json_catalogue(self):
        with patch.object(self.store,'export',side_effect=AssertionError('unexpected complete JSON export')):
            self.publish()
        self.assertEqual(self.values(),['a'])
        self.assertFalse((self.store.root/'manifest.json').exists())

    def test_large_catalogue_routes_through_durable_stage(self):
        self.large()
        with patch.object(a,'s3_client',return_value=self.client),patch.object(s,'publish') as publish:
            a._publish_snapshot(self.store,'vn-archive','mirrors')
        publish.assert_called_once_with(self.store,self.client,'vn-archive','mirrors',compressed=True)


if __name__=='__main__':unittest.main()


class CompressedSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,"https://source.test/items/a","lathercraft")[0]
        self.record["id"]="a";self.store.add(self.record)
        self.client=VersionedGateway();self.key="index.sqlite3.gz"

    def publish(self,readback=None):
        def proof(*args):
            self.assertEqual(args[-1],self.key)
            body=self.client.objects[self.key]
            return hashlib.sha256(body).hexdigest(),len(body)
        with patch.object(a,"public_digest",side_effect=readback or proof):
            s.publish(self.store,self.client,"vn-archive","mirrors",compressed=True)

    def pointer(self):return s.slot(self.store.root,"vn-archive","mirrors",self.key)/"current.json"

    def values(self):
        import catalogue_transport as c
        import io
        path=self.store.root/"restored.sqlite3"
        proof=c.restore(io.BytesIO(self.client.objects[self.key]),path,reserve=0)
        with closing(sqlite3.connect(path)) as db:
            return [row[0] for row in db.execute("SELECT id FROM items ORDER BY id")],proof

    def test_compressed_publication_retains_raw_proof_and_does_not_replace_raw_object(self):
        self.client.objects["index.sqlite3"]=b"legacy snapshot"
        self.publish();rows,proof=self.values();self.assertEqual(rows,["a"])
        receipt=json.loads((self.pointer().parent/"last-verified.json").read_text())
        self.assertEqual(receipt["raw_sha256"],proof["sha256"])
        self.assertEqual(receipt["raw_size"],proof["size"])
        self.assertEqual(self.client.objects["index.sqlite3"],b"legacy snapshot")
        self.assertFalse(self.pointer().exists())

    def test_failed_readback_resumes_same_frozen_generation(self):
        with self.assertRaises(TimeoutError):
            self.publish(readback=lambda *args:(_ for _ in ()).throw(TimeoutError()))
        first=json.loads(self.pointer().read_text());sent=list(self.client.sent)
        self.store.add(dict(self.record,id="b"))
        with patch.object(a,"_compact_snapshot",side_effect=AssertionError("generation rebuilt")):
            self.publish()
        self.assertEqual(self.client.sent,sent);self.assertEqual(self.values()[0],["a"])
        self.assertFalse(m.snapshot_path(self.store.root,"vn-archive","mirrors",first["sha256"],self.key).exists())

    def test_compressed_destination_claim_is_independent_and_exclusive(self):
        raw=hashlib.sha256(m.json_identity("vn-archive","mirrors").encode()).hexdigest()
        gz=hashlib.sha256(m.json_identity("vn-archive","mirrors",self.key).encode()).hexdigest()
        with exclusive_file(self.store.root/".claims"/("snapshot-"+raw+".lock")):
            self.publish()
        with exclusive_file(self.store.root/".claims"/("snapshot-"+gz+".lock")),self.assertRaises(BlockingIOError):
            self.publish()

    def test_compressed_snapshot_requires_its_exact_managed_path(self):
        path=self.store.root/"index.sqlite3"
        with self.assertRaisesRegex(ValueError,"immutable managed staging"):
            m.transfer_snapshot_and_readback(self.store,self.client,"vn-archive","mirrors",path,"0"*64,path.stat().st_size,lambda:None,key=self.key)
        with self.assertRaises(ValueError):m.snapshot_path(self.store.root,"vn-archive","mirrors","0"*64,"arbitrary.gz")
        self.assertEqual(self.client.created,0)
