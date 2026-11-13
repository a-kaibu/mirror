import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock,patch
import archiver as a
import bulk
from test_archiver import HTML,PAYLOAD,Fetch,Response


class PublicationMetadataRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://source.test/item','lathercraft')[0]
        self.store.add(self.record)
        self.assertEqual(a.download(self.store,Fetch([Response(PAYLOAD)]),export=False),0)
        self.sha=hashlib.sha256(PAYLOAD).hexdigest()

    def publish_during_update(self,change):
        def readback(*args):
            fresh=json.loads(json.dumps(self.record));change(fresh);self.store.add(fresh)
            return self.sha,len(PAYLOAD)
        client=Mock()
        with patch.object(a,'s3_client',return_value=client),patch.object(a,'public_digest',side_effect=readback) as digest:
            result=a.publish(self.store,'vn-archive','mirrors',metadata=False)
        self.assertEqual(digest.call_count,1)
        return result

    def test_descriptive_change_keeps_full_readback_proof_bound_to_current_hash(self):
        self.assertEqual(self.publish_during_update(lambda record:record.update(title='Updated title')),1)
        row=self.store.db.execute('SELECT * FROM items').fetchone()
        self.assertTrue(bulk.publicly_archived(row,'vn-archive','mirrors'))
        self.assertEqual(json.loads(row['data'])['title'],'Updated title')

    def test_changed_publisher_constraints_and_quarantine_do_not_get_proof(self):
        changes=[{'expected_md5':'0'*32},{'expected_sha256':'0'*64},{'expected_size':len(PAYLOAD)+1},
                 {'expected_md5':None,'expected_sha256':None,'title':'No checksum'},
                 {'fields':{'source_association_quarantined':True}}]
        for change in changes:
            with self.subTest(change=change):
                self.store.add(self.record)
                # Reset the fixture to its independently verified local bytes.
                row=self.store.db.execute('SELECT * FROM items').fetchone()
                self.store.update(row['id'],status='verified',remote_sha256=None,remote_verified_at=None)
                with self.assertRaisesRegex(ValueError,'metadata changed during publication'):
                    self.publish_during_update(lambda record:record.update(change))
                current=self.store.db.execute('SELECT * FROM items').fetchone()
                self.assertFalse(bulk.publicly_archived(current,'vn-archive','mirrors'))
                self.assertTrue((self.store.root/current['path']).is_file())

    def test_second_metadata_race_cannot_bypass_compare_and_swap(self):
        update=self.store.update_if_current;calls=0
        def racing(ident,expected,**fields):
            nonlocal calls
            calls+=1
            if calls==2:
                fresh=json.loads(self.store.db.execute('SELECT data FROM items WHERE id=?',(ident,)).fetchone()[0]);fresh['expected_md5']='0'*32;self.store.add(fresh)
            return update(ident,expected,**fields)
        with patch.object(self.store,'update_if_current',side_effect=racing):
            with self.assertRaisesRegex(ValueError,'metadata changed during publication'):
                self.publish_during_update(lambda record:record.update(title='Updated title'))
        self.assertFalse(bulk.publicly_archived(self.store.db.execute('SELECT * FROM items').fetchone(),'vn-archive','mirrors'))


if __name__=='__main__':unittest.main()
