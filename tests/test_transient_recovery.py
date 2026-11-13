from datetime import datetime,timezone,timedelta
import hashlib
import tempfile
import unittest
import archiver as a
import transient_recovery as r
from archive_lock import exclusive_file
from test_archiver import HTML


class TransientRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.record=a.parse_detail(HTML,'https://source.test/item','kokoron')[0];self.store.add(self.record)
        self.ident=self.record['id'];self.at=datetime(2026,11,13,12,tzinfo=timezone.utc)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def fail(self,error,age):
        self.store.update(self.ident,status='failed',error=error)
        self.store.db.execute('INSERT INTO attempts(item_id,url,attempted_at,error) VALUES(?,?,?,?)',(self.ident,'https://source.test/demo.zip',(self.at-timedelta(hours=age)).isoformat(),error));self.store.db.commit()
    def test_only_temporary_errors_are_eligible(self):
        for error in ['HTTP Error 429: Too Many Requests','HTTP Error 503: Service Unavailable','<urlopen error timed out>','Remote end closed connection without response','<urlopen error [Errno -3] Temporary failure in name resolution>']:
            self.assertTrue(r.temporary(error),error)
        for error in ['HTTP Error 404: Not Found','MD5 mismatch','server returned HTML for a download','<urlopen error [Errno -2] Name or service not known>','certificate verify failed']:
            self.assertFalse(r.temporary(error),error)
    def test_cooldown_and_requeue_receipt_preserve_attempt_evidence(self):
        self.fail('HTTP Error 429: Too Many Requests',2)
        self.assertEqual(r.recover(self.store,self.at)['cooling'],1)
        self.assertEqual(r.recover(self.store,self.at+timedelta(hours=2))['requeued'],1)
        row=self.store.db.execute('SELECT status,error FROM items WHERE id=?',(self.ident,)).fetchone()
        self.assertEqual(tuple(row),('pending',None))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT reason FROM transient_retries').fetchone()[0],'HTTP Error 429: Too Many Requests')
    def test_repeated_failure_backs_off_and_same_attempt_is_not_requeued_twice(self):
        self.fail('<urlopen error timed out>',4)
        self.assertEqual(r.recover(self.store,self.at)['requeued'],1)
        self.store.update(self.ident,status='failed',error='<urlopen error timed out>')
        self.assertEqual(r.recover(self.store,self.at+timedelta(days=1))['requeued'],0)
        self.fail('<urlopen error timed out>',0)
        self.assertEqual(r.recover(self.store,self.at+timedelta(hours=4))['cooling'],1)
        self.assertEqual(r.recover(self.store,self.at+timedelta(hours=7))['requeued'],1)
    def test_busy_transfer_is_deferred_and_verified_file_is_untouched(self):
        self.fail('HTTP Error 503: Service Unavailable',4)
        with exclusive_file(self.store.root/'.claims'/(hashlib.sha256(self.ident.encode()).hexdigest()+'.lock')):
            self.assertEqual(r.recover(self.store,self.at)['deferred'],1)
        self.store.update(self.ident,status='verified')
        self.assertEqual(r.recover(self.store,self.at)['requeued'],0)
    def test_permanent_failure_missing_attempt_or_future_timestamp_stays_failed(self):
        self.fail('HTTP Error 404: Not Found',20)
        self.assertEqual(r.recover(self.store,self.at)['requeued'],0)
        self.store.db.execute('DELETE FROM attempts');self.store.db.commit()
        self.store.update(self.ident,error='timed out')
        self.assertEqual(r.recover(self.store,self.at)['requeued'],0)
        self.fail('timed out',-5)
        self.assertEqual(r.recover(self.store,self.at)['cooling'],1)

    def test_temporary_archive_error_is_not_hidden_by_dead_live_mirror(self):
        self.fail('HTTP Error 429: Too Many Requests',5)
        self.store.db.execute('INSERT INTO attempts(item_id,url,attempted_at,error) VALUES(?,?,?,?)',(self.ident,'https://dead.test/demo.zip',(self.at-timedelta(hours=4)).isoformat(),'<urlopen error [Errno -2] Name or service not known>'));self.store.db.commit()
        self.store.update(self.ident,error='<urlopen error [Errno -2] Name or service not known>')
        self.assertEqual(r.recover(self.store,self.at)['requeued'],1)
        self.assertEqual(self.store.db.execute('SELECT reason FROM transient_retries').fetchone()[0],'HTTP Error 429: Too Many Requests')

    def test_old_transient_error_for_same_url_does_not_override_new_permanent_error(self):
        self.fail('HTTP Error 429: Too Many Requests',6)
        self.fail('HTTP Error 404: Not Found',5)
        self.assertEqual(r.recover(self.store,self.at)['requeued'],0)
