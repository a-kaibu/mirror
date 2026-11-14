import hashlib
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import reuse_recovery as r
from test_archiver import HTML,PAYLOAD


class ReuseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.sha=hashlib.sha256(PAYLOAD).hexdigest();self.md5=hashlib.md5(PAYLOAD).hexdigest()
        self.add('donor');self.store.update('donor',status='archived',md5=self.md5,sha256=self.sha,size=len(PAYLOAD),remote_sha256=self.sha,remote_key='vn-archive/mirrors/files/'+self.sha,remote_verified_at=a.now())
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def add(self,ident,brand='Brand'):
        record=a.parse_detail(HTML,'https://source.test/'+ident,'kokoron')[0]
        record.update(id=ident,brand=brand);self.store.add(record);return record
    def row(self,ident):return self.store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone()

    def test_new_public_donor_recovers_failed_observation_without_network(self):
        self.add('target');self.store.update('target',status='failed',error='old mirror closed')
        with patch.object(self.store,'export',side_effect=AssertionError('background recovery must not render the catalogue')):
            self.assertEqual(r.recover(self.store),{'reused':1,'deferred':0,'unresolved':0})
        row=self.row('target');self.assertEqual(row['status'],'archived');self.assertEqual(row['sha256'],self.sha)
        self.assertEqual(row['reused_from'],'donor');self.assertIsNone(row['error'])
        self.assertEqual(r.recover(self.store)['reused'],0)

    def test_reuse_does_not_overwrite_known_matching_build_sha256(self):
        self.add('target');self.store.update('target',sha256='f'*64,md5=self.md5,size=len(PAYLOAD))
        self.assertEqual(r.recover(self.store)['unresolved'],1)
        self.assertEqual(self.row('target')['sha256'],'f'*64)
        self.assertEqual(self.row('target')['status'],'pending')

    def test_busy_claim_is_deferred_and_retried(self):
        self.add('target')
        with patch.object(r,'exclusive_file',side_effect=BlockingIOError('busy')):
            self.assertEqual(r.recover(self.store),{'reused':0,'deferred':1,'unresolved':0})
        self.assertEqual(r.recover(self.store)['reused'],1)

    def test_foreign_maker_size_and_bucket_are_not_reused(self):
        self.add('other',brand='Other');record=self.add('wrongsize');record['expected_size']+=1;self.store.add(record)
        self.assertEqual(r.recover(self.store)['reused'],0)
        self.add('target');self.assertEqual(r.recover(self.store,bucket='elsewhere')['reused'],0)

    def test_posted_sha_recovers_without_manufacturer_or_md5(self):
        record=self.add('target',brand=None);record.update(expected_md5=None,expected_sha256=self.sha);self.store.add(record)
        self.assertEqual(r.recover(self.store),{'reused':1,'deferred':0,'unresolved':0})
        self.assertEqual(self.row('target')['reused_from'],'donor')

    def test_overlapping_md5_and_sha_matches_defer_once(self):
        record=self.add('target');record['expected_sha256']=self.sha;self.store.add(record)
        with patch.object(r,'exclusive_file',side_effect=BlockingIOError('busy')):
            self.assertEqual(r.recover(self.store),{'reused':0,'deferred':1,'unresolved':0})

    def test_sha_recovery_does_not_ignore_conflicting_md5_size_or_bucket(self):
        for change in [{'expected_md5':'0'*32},{'expected_size':len(PAYLOAD)+1}]:
            record=self.add('target',brand='Other');record.update(expected_sha256=self.sha);record.update(change);self.store.add(record)
            self.assertEqual(r.recover(self.store)['reused'],0)
        record=self.add('target',brand='Other');record.update(expected_sha256=self.sha);self.store.add(record)
        self.assertEqual(r.recover(self.store,bucket='elsewhere')['reused'],0)
