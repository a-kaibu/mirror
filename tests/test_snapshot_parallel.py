import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import archiver as a
import multipart_upload as m
from test_multipart_upload import Gateway


class ParallelSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        self.body=Path(tmp.name)/'immutable';self.payload=b'abcdefghijklmnopqrstuvwxyz!';self.body.write_bytes(self.payload)
        self.sha=hashlib.sha256(self.payload).hexdigest();self.client=Gateway();self.reads=0
        p=patch.object(m,'MIN_PART_SIZE',1);p.start();self.addCleanup(p.stop)
    def readback(self):
        self.reads+=1;body=self.client.objects['index.sqlite3.gz']
        return hashlib.sha256(body).hexdigest(),len(body)
    def send(self):
        with m._checkpoints(self.store.root) as cache:
            return m._transfer_and_readback(cache,self.client,'vn-archive','mirrors','index.sqlite3.gz',self.body,self.sha,len(self.payload),self.readback,8,snapshot=True)
    def test_two_workers_out_of_order_parts_complete_in_order(self):
        base=self.client.upload_part;barrier=threading.Barrier(2);second=threading.Event();lock=threading.Lock()
        active=0;peak=0;peak_bytes=0;held_bytes=0
        def send(**kw):
            nonlocal active,peak,held_bytes,peak_bytes
            with lock:
                active+=1;held_bytes+=len(kw['Body']);peak=max(peak,active);peak_bytes=max(peak_bytes,held_bytes)
            try:
                number=kw['PartNumber']
                if number<=2:barrier.wait(3)
                if number==1:
                    if not second.wait(3):raise AssertionError('second part did not finish first')
                result=base(**kw)
                if number==2:second.set()
                return result
            finally:
                with lock:active-=1;held_bytes-=len(kw['Body'])
        self.client.upload_part=send
        self.assertEqual(self.send(),(self.sha,len(self.payload)))
        self.assertEqual(peak,2);self.assertLessEqual(peak_bytes,16);self.assertEqual(active,0)
        self.assertEqual([part['PartNumber'] for part in self.client.completed[0]],[1,2,3,4])
        self.assertEqual(self.reads,1)
    def test_failed_part_drains_successful_sibling_and_resume_does_not_resend_it(self):
        base=self.client.upload_part;second=threading.Event();failed=False;active=0;lock=threading.Lock()
        def send(**kw):
            nonlocal failed,active
            with lock:active+=1
            try:
                number=kw['PartNumber']
                if number==1 and not failed:
                    failed=True
                    if not second.wait(3):raise AssertionError('second part not sent')
                    self.client.sent.append(1);raise TimeoutError('first part failed')
                result=base(**kw)
                if number==2:second.set()
                return result
            finally:
                with lock:active-=1
        self.client.upload_part=send
        with self.assertRaises(TimeoutError):self.send()
        self.assertEqual(active,0);self.assertEqual(self.client.completed,[])
        with m._checkpoints(self.store.root) as cache:
            saved=cache.db.execute("SELECT number FROM _s3_upload_parts WHERE key='index.sqlite3.gz'").fetchall()
        self.assertIn(2,[row[0] for row in saved]);self.assertEqual(self.client.sent.count(2),1)
        self.assertEqual(self.send(),(self.sha,len(self.payload)))
        self.assertEqual(self.client.created,1);self.assertEqual(self.client.sent.count(2),1)
        self.assertEqual(self.reads,1);self.assertEqual(active,0)
