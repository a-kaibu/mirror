import hashlib
import json
import tempfile
import unittest
import archiver as a
from archive_lock import exclusive_file
from archive_watch import queue_recovered_mirrors
from test_archiver import HTML
import test_healthy_mirrors as hm


class RecoveredRequeueTests(unittest.TestCase):
    def test_requeues_untried_original_once_without_changing_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            store,record=hm.HealthyMirrorTests().setup_store(root)
            try:
                store.update(record['id'],status='failed',error='old capture unavailable')
                before=store.db.execute('SELECT data FROM items').fetchone()[0]
                self.assertEqual(queue_recovered_mirrors(store,'suezou'),1)
                row=store.db.execute('SELECT * FROM items').fetchone()
                self.assertEqual((row['status'],row['error'],row['data']),('pending',None,before))
                self.assertEqual(store.db.execute('SELECT url FROM recovered_mirror_requeues').fetchone()[0],hm.HealthyMirrorTests.raw)
                store.update(record['id'],status='failed')
                self.assertEqual(queue_recovered_mirrors(store,'suezou'),0)
            finally:store.db.close()

    def test_previous_original_attempt_prevents_repeated_retry(self):
        with tempfile.TemporaryDirectory() as root:
            store,record=hm.HealthyMirrorTests().setup_store(root)
            try:
                store.update(record['id'],status='failed')
                store.db.execute('INSERT INTO attempts(item_id,url,attempted_at,error) VALUES(?,?,?,?)',(record['id'],hm.HealthyMirrorTests.raw,a.now(),'checksum mismatch'));store.db.commit()
                self.assertEqual(queue_recovered_mirrors(store,'suezou'),0)
            finally:store.db.close()

    def test_other_source_uses_only_checksum_matched_observed_donor(self):
        with tempfile.TemporaryDirectory() as root:
            store,donor=hm.HealthyMirrorTests().setup_store(root)
            try:
                target=a.parse_detail(HTML,a.replay('http://closed.test/game','20100102030405'),'holyseal')[0]
                target.update(mirrors=[a.replay('http://closed.test/demo.zip','20100102030405',raw=True)])
                store.add(target);store.update(target['id'],status='failed')
                self.assertEqual(queue_recovered_mirrors(store,'holyseal'),1)
                store.update(target['id'],status='failed')
                store.db.execute('DELETE FROM recovered_mirror_requeues')
                changed=dict(donor,expected_md5='0'*32);store.db.execute('UPDATE items SET data=? WHERE id=?',(json.dumps(changed),donor['id']));store.db.commit()
                self.assertEqual(queue_recovered_mirrors(store,'holyseal'),0)
            finally:store.db.close()

    def test_skips_busy_hashless_quarantined_and_stale_health(self):
        for reason in ['busy','hashless','quarantined','stale']:
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as root:
                store,record=hm.HealthyMirrorTests().setup_store(root,hash_bound=reason!='hashless')
                try:
                    store.update(record['id'],status='failed')
                    if reason=='quarantined':
                        record['fields']={**record.get('fields',{}),'source_association_quarantined':True}
                        store.db.execute('UPDATE items SET data=?',(json.dumps(record),));store.db.commit()
                    if reason=='stale':
                        store.db.execute("UPDATE source_health SET checked_at='2000-01-01T00:00:00+00:00'");store.db.commit()
                    if reason=='busy':
                        with exclusive_file(store.root/'.claims'/(hashlib.sha256(record['id'].encode()).hexdigest()+'.lock'),blocking=False):
                            self.assertEqual(queue_recovered_mirrors(store,'suezou'),0)
                    else:self.assertEqual(queue_recovered_mirrors(store,'suezou'),0)
                    self.assertEqual(store.db.execute('SELECT status FROM items').fetchone()[0],'failed')
                finally:store.db.close()
