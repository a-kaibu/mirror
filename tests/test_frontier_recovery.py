from datetime import datetime,timedelta,timezone
import tempfile
import unittest

import archiver as a
import mirror_sources as m
from archive_lock import exclusive_file
from transient_recovery import recover_frontiers


class FrontierRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close);m.initialize(self.store)
        self.at=datetime(2026,11,14,12,tzinfo=timezone.utc)
    def add(self,name,error='timed out',hours=4,mode='archive',site='kokoron',stamp=None):
        url='https://source.test/'+name
        checked=stamp if stamp is not None else (self.at-timedelta(hours=hours)).isoformat()
        self.store.db.execute("INSERT OR REPLACE INTO mirror_frontier(site,url,mode,status,checked_at,error,path) VALUES(?,?,?,'failed',?,?,?)",(site,url,mode,checked,error,'html/saved.html'));self.store.db.commit()
        return url
    def state(self,url,mode='archive'):
        return self.store.db.execute('SELECT status,error,path FROM mirror_frontier WHERE url=? AND mode=?',(url,mode)).fetchone()
    def test_requeues_only_temporary_errors_and_preserves_saved_html(self):
        transient=[self.add(str(n),error=e) for n,e in enumerate(['timed out','HTTP Error 429: slow down','HTTP Error 503: unavailable'])]
        permanent=[self.add('p'+str(n),error=e) for n,e in enumerate(['HTTP Error 404: missing','MD5 mismatch','archive redirected outside registered hosts'])]
        stats=recover_frontiers(self.store,self.at)
        self.assertEqual(stats['requeued'],3)
        for url in transient:self.assertEqual(tuple(self.state(url)),('pending',None,'html/saved.html'))
        for url in permanent:self.assertEqual(self.state(url)['status'],'failed')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM frontier_transient_retries').fetchone()[0],3)
    def test_cooldown_future_and_invalid_timestamps(self):
        self.add('recent',hours=2);self.add('future',hours=-1)
        for value in ['invalid','2026-11-01T00:00:00']:
            self.add(value,stamp=value)
        stats=recover_frontiers(self.store,self.at)
        self.assertEqual(stats,{'requeued':0,'deferred':0,'cooling':2})
        with self.assertRaises(ValueError):recover_frontiers(self.store,self.at,cooldown=0)
    def test_a_failed_observation_is_requeued_once(self):
        url=self.add('once')
        self.assertEqual(recover_frontiers(self.store,self.at)['requeued'],1)
        self.assertEqual(recover_frontiers(self.store,self.at)['requeued'],0)
        self.store.db.execute("UPDATE mirror_frontier SET status='failed',error='timed out' WHERE url=?",(url,));self.store.db.commit()
        self.assertEqual(recover_frontiers(self.store,self.at+timedelta(days=3))['requeued'],0)
    def test_new_failure_uses_exponential_cooldown_and_separate_modes(self):
        url=self.add('retry');recover_frontiers(self.store,self.at)
        self.add('retry',hours=5)
        self.assertEqual(recover_frontiers(self.store,self.at)['cooling'],1)
        self.assertEqual(recover_frontiers(self.store,self.at+timedelta(hours=1))['requeued'],1)
        self.add('retry',mode='live')
        self.assertEqual(recover_frontiers(self.store,self.at)['requeued'],1)
    def test_active_source_claim_defers_without_audit_or_state_changes(self):
        url=self.add('busy')
        with exclusive_file(self.store.root/'.claims'/'discovery-kokoron.lock'):
            self.assertEqual(recover_frontiers(self.store,self.at),{'requeued':0,'deferred':1,'cooling':0})
        self.assertEqual(self.state(url)['status'],'failed')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM frontier_transient_retries').fetchone()[0],0)
        self.assertEqual(recover_frontiers(self.store,self.at)['requeued'],1)
    def test_absent_frontier_is_a_noop(self):
        self.store.db.execute('DROP TABLE mirror_frontier');self.store.db.commit()
        self.assertEqual(recover_frontiers(self.store,self.at),{'requeued':0,'deferred':0,'cooling':0})
