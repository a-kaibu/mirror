import json
import tempfile
import unittest
from urllib.error import URLError
import archiver as a
from test_archiver import Fetch,Response,HTML,PAYLOAD


class HealthyMirrorTests(unittest.TestCase):
    raw='http://suezou.dyndns.org/dl2020/test/demo.zip'
    replay=a.replay(raw,'20200102030405',raw=True)

    def setup_store(self,root,hash_bound=True):
        store=a.Store(root)
        record=a.parse_detail(HTML,'https://example.test/items/1','lathercraft')[0]
        record.update(site='suezou',filename='demo.zip',mirrors=[self.replay])
        if not hash_bound:record['expected_md5']=None
        store.add(record)
        store.db.execute('CREATE TABLE source_health(site TEXT PRIMARY KEY,checked_at TEXT,healthy INTEGER,data TEXT)')
        store.db.execute('INSERT INTO source_health VALUES(?,?,1,?)',('suezou',a.now(),json.dumps({'url':'http://suezou.dyndns.org/','healthy':True})));store.db.commit()
        return store,record

    def test_digest_bound_historical_file_tries_recovered_original_first(self):
        with tempfile.TemporaryDirectory() as root:
            store,record=self.setup_store(root)
            fetch=Fetch([Response(PAYLOAD,url=self.raw)])
            try:
                self.assertEqual(a.download(store,fetch,'suezou',export=False),0)
                self.assertEqual(fetch.requests[0][0],self.raw)
                row=store.db.execute('SELECT * FROM items').fetchone()
                self.assertEqual(row['source_url'],self.raw)
                self.assertEqual(json.loads(row['data'])['mirrors'],[self.replay])
            finally:store.db.close()

    def test_hashless_historical_record_keeps_capture_first(self):
        with tempfile.TemporaryDirectory() as root:
            store,_=self.setup_store(root,hash_bound=False)
            fetch=Fetch([Response(PAYLOAD,url=self.replay)])
            try:
                self.assertEqual(a.download(store,fetch,'suezou',export=False),0)
                self.assertEqual(fetch.requests[0][0],self.replay)
            finally:store.db.close()

    def test_failed_live_attempt_preserves_the_original_capture_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            store,_=self.setup_store(root)
            fetch=Fetch([URLError(ConnectionRefusedError('refused')),Response(PAYLOAD,url=self.replay)])
            try:
                self.assertEqual(a.download(store,fetch,'suezou',export=False),0)
                self.assertEqual([r[0] for r in fetch.requests],[self.raw,self.replay])
                row=store.db.execute('SELECT status,source_url FROM items').fetchone()
                self.assertEqual(tuple(row),('verified',self.replay))
            finally:store.db.close()

    def test_stale_unhealthy_or_invalid_health_does_not_prefer_live(self):
        with tempfile.TemporaryDirectory() as root:
            store,_=self.setup_store(root)
            try:
                for checked,healthy,data in [('2000-01-01T00:00:00+00:00',1,{'url':'http://suezou.dyndns.org/'}),
                                              (a.now(),0,{'url':'http://suezou.dyndns.org/'}),
                                              (a.now(),1,[]),('invalid',1,{'url':self.raw})]:
                    store.db.execute('UPDATE source_health SET checked_at=?,healthy=?,data=?',(checked,healthy,json.dumps(data)));store.db.commit()
                    self.assertEqual(a.healthy_source_mirrors(store,'suezou',[self.replay]),[])
            finally:store.db.close()

    def test_checksum_bound_other_site_uses_observed_recovered_donor(self):
        with tempfile.TemporaryDirectory() as root:
            store,donor=self.setup_store(root)
            try:
                target=a.parse_detail(HTML,a.replay('http://closed.test/game','20100102030405'),'holyseal')[0]
                target.update(filename='demo.zip',mirrors=[a.replay('http://closed.test/demo.zip','20100102030405',raw=True)])
                store.add(target);fetch=Fetch([Response(PAYLOAD,url=self.raw)])
                self.assertEqual(a.download(store,fetch,'holyseal',export=False),0)
                self.assertEqual(fetch.requests[0][0],self.raw)
                row=store.db.execute('SELECT * FROM items WHERE id=?',(target['id'],)).fetchone()
                self.assertEqual(row['source_url'],self.raw)
                self.assertEqual(json.loads(row['data'])['mirrors'],target['mirrors'])
            finally:store.db.close()

    def test_invalid_health_does_not_hide_another_fresh_host(self):
        with tempfile.TemporaryDirectory() as root:
            store,_=self.setup_store(root)
            try:
                store.db.execute("UPDATE source_health SET checked_at='invalid'")
                store.db.execute('INSERT INTO source_health VALUES(?,?,1,?)',('other',a.now(),json.dumps({'url':'https://healthy.test/'})));store.db.commit()
                raw='https://healthy.test/trial.zip'
                self.assertEqual(a.healthy_source_mirrors(store,'suezou',[self.replay,a.replay(raw,'20100102030405',raw=True)]),[raw])
            finally:store.db.close()

    def test_health_never_invents_a_url_on_a_recovered_host(self):
        with tempfile.TemporaryDirectory() as root:
            store,_=self.setup_store(root)
            try:
                self.assertEqual(a.healthy_source_mirrors(store,'holyseal',[a.replay('http://other.test/demo.zip','20100102030405',raw=True)]),[])
            finally:store.db.close()

    def test_multiple_healthy_hosts_keep_source_site_first(self):
        with tempfile.TemporaryDirectory() as root:
            store,_=self.setup_store(root)
            try:
                store.db.execute('INSERT INTO source_health VALUES(?,?,1,?)',('other',a.now(),json.dumps({'url':'https://healthy.test/'})));store.db.commit()
                other='https://healthy.test/trial.zip'
                self.assertEqual(a.healthy_source_mirrors(store,'suezou',iter([other,self.replay])),[self.raw,other])
            finally:store.db.close()
