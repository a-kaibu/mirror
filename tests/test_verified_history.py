import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import catalog
from test_archiver import HTML,PAYLOAD


class VerifiedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.record=a.parse_detail(HTML,'https://source.test/item','multi_inside')[0]
        self.record.update(expected_md5=None,expected_size=None)
        self.store.add(self.record);self.sha=hashlib.sha256(PAYLOAD).hexdigest()
        self.store.update(self.record['id'],status='archived',sha256=self.sha,md5=hashlib.md5(PAYLOAD).hexdigest(),size=len(PAYLOAD),remote_key='vn-archive/mirrors/files/'+self.sha,remote_sha256=self.sha,remote_verified_at=a.now(),source_url=self.record['mirrors'][0])
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def row(self):return self.store.db.execute('SELECT * FROM items WHERE id=?',(self.record['id'],)).fetchone()
    def changed(self):
        data=copy.deepcopy(self.record);data['mirrors']=['https://new.test/demo.zip'];return data
    def test_source_change_preserves_old_download_and_clears_new_observation_bytes(self):
        old=dict(self.row());data=self.changed();self.store.add(data)
        current=self.row();self.assertEqual(current['status'],'pending');self.assertIsNone(current['sha256'])
        history=self.store.db.execute('SELECT * FROM items WHERE id!=?',(data['id'],)).fetchone()
        self.assertEqual(history['remote_sha256'],self.sha);self.assertEqual(history['remote_key'],old['remote_key']);self.assertIsNone(history['path'])
        saved=json.loads(history['data']);self.assertEqual(saved['mirrors'],self.record['mirrors'])
        self.assertEqual(saved['fields']['verified_history_of'],data['id'])
        files=catalog.manifest(self.store)['files'];self.assertEqual(len(files),2)
        self.assertEqual(sum(bool(f['download_url']) for f in files),1)
        self.store.add(data);self.assertEqual(len(self.store.rows()),2)
    def test_descriptive_update_keeps_current_proof_without_extra_history(self):
        data=copy.deepcopy(self.record);data['fields']['note']='corrected';self.store.add(data)
        self.assertEqual(self.row()['remote_sha256'],self.sha);self.assertEqual(len(self.store.rows()),1)
    def test_snapshot_and_source_update_roll_back_together(self):
        before=dict(self.row())
        with patch.object(catalog,'index_record',side_effect=RuntimeError('catalog failure')):
            with self.assertRaisesRegex(RuntimeError,'catalog failure'):self.store.add(self.changed())
        self.assertEqual(dict(self.row()),before);self.assertEqual(len(self.store.rows()),1)
    def test_unpublished_bytes_are_not_claimed_as_public_history(self):
        self.store.update(self.record['id'],remote_sha256=None,remote_verified_at=None)
        self.store.add(self.changed());self.assertEqual(len(self.store.rows()),1)
        self.assertIsNone(self.row()['remote_sha256'])
    def test_same_bytes_history_is_deduplicated_after_current_observation_is_verified(self):
        self.store.add(self.changed())
        self.store.update(self.record['id'],status='archived',sha256=self.sha,size=len(PAYLOAD),remote_key='vn-archive/mirrors/files/'+self.sha,remote_sha256=self.sha,remote_verified_at=a.now())
        files=catalog.manifest(self.store)['files'];self.assertEqual(len(files),1);self.assertTrue(files[0]['download_url'])
