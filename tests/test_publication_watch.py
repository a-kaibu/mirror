import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import archiver as a
import publication_watch as w
from archive_lock import exclusive_file
from test_archiver import HTML,PAYLOAD

class PublicationWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.client=Mock();self.objects={}
        self.client.upload_file.side_effect=lambda path,bucket,key,**kw:self.objects.update({key:Path(path).read_bytes()})
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def add(self,ident):
        record=a.parse_detail(HTML,'https://source.test/items/'+ident,'lathercraft')[0]
        record.update(id=ident,filename=ident+'.zip');self.store.add(record)
        path=self.store.root/'files'/(ident+'.zip');path.parent.mkdir(exist_ok=True);path.write_bytes(PAYLOAD)
        self.store.update(ident,status='verified',path=path.relative_to(self.store.root).as_posix(),**a.digest(path))
        return record,path
    def digest(self,namespace,bucket,key):
        return hashlib.sha256(self.objects[key]).hexdigest(),len(self.objects[key])
    def test_busy_download_is_skipped_and_other_file_publishes_without_source_download(self):
        busy,busy_path=self.add('busy');free,free_path=self.add('free')
        claim=self.store.root/'.claims'/(hashlib.sha256(busy['id'].encode()).hexdigest()+'.lock')
        with patch.object(a,'download',side_effect=AssertionError('publication must not download sources')),patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',side_effect=self.digest):
            with exclusive_file(claim):result=w.publish_pending(self.store,limit=1)
            self.assertEqual((result['published'],result['busy'],result['failed']),(1,1,0))
            self.assertTrue(busy_path.exists());self.assertFalse(free_path.exists())
            resumed=w.publish_pending(self.store)
            self.assertEqual(resumed['published'],1)
            self.assertFalse(busy_path.exists())
            self.assertEqual(w.publish_pending(self.store)['processed'],0)
    def test_readback_failure_retains_local_file_and_next_pass_recovers(self):
        record,path=self.add('file')
        with patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',return_value=('wrong',1)):
            result=w.publish_pending(self.store)
        self.assertEqual(result['failed'],1);self.assertTrue(path.exists())
        self.assertIsNone(self.store.rows()[0]['remote_verified_at'])
        with patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',side_effect=self.digest):
            self.assertEqual(w.publish_pending(self.store)['published'],1)
        self.assertFalse(path.exists())
    def test_quarantined_observation_is_not_published(self):
        record,path=self.add('quarantined');record.setdefault('fields',{})['source_association_quarantined']=True;self.store.add(record)
        with patch.object(a,'publish') as publish:result=w.publish_pending(self.store)
        publish.assert_not_called();self.assertEqual(result['quarantined'],1);self.assertTrue(path.exists())
    def test_keep_local_is_preserved_after_publication_and_repeated_pass(self):
        record,path=self.add('keep')
        with patch.object(a,'s3_client',return_value=self.client),patch.object(a,'public_digest',side_effect=self.digest):
            result=w.publish_pending(self.store,keep_local=True)
            self.assertEqual(result['published'],1);self.assertEqual(result['evicted_bytes'],0)
            self.assertEqual(w.publish_pending(self.store,keep_local=True)['processed'],0)
        self.assertTrue(path.exists())
    def test_upload_blocking_error_is_reported_as_failure_not_busy_claim(self):
        record,path=self.add('failed')
        with patch.object(a,'publish',side_effect=BlockingIOError('upload failed')):result=w.publish_pending(self.store)
        self.assertEqual((result['failed'],result['busy']),(1,0));self.assertTrue(path.exists())
