import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from email.message import Message
import archiver as a
import redirect_recovery as r
from test_archiver import HTML,Fetch,Response


class RedirectRecoveryTests(unittest.TestCase):
    target='http://mirror.test/download.php?id=1'
    capture='http://web.archive.org/web/20070102030405id_/http://mirror.test/download.php?id=1'
    binary='http://dl.mirror.test:8081/files/trial.exe'

    def payload(self):
        return [['timestamp','original','mimetype','statuscode','digest'],
            ['20070102030405',self.target,'text/html','302','same-body'],
            ['20080102030405',self.target,'text/html','302','same-body'],
            ['20090102030405',self.target,'text/html','404','missing'],
            ['20090102030405',self.target+'2','text/html','302','wrong-url']]

    def test_redirect_body_digests_do_not_hide_changed_locations(self):
        urls=r.captures(self.payload(),self.target,scheme='http')
        self.assertEqual(len(urls),2)
        self.assertIn('20070102030405',urls[0]);self.assertIn('20080102030405',urls[1])
        self.assertEqual(r.captures(self.payload(),self.target.replace('mirror.test','different.test')),[])

    def test_observed_target_keeps_nondefault_port_without_borrowing_timestamp(self):
        observation={'status':302,'location':a.replay(self.binary,'20070102030405',raw=True,scheme='http'),'response_url':self.capture}
        self.assertEqual(r.file_target(observation),self.binary)
        self.assertIsNone(r.file_target({**observation,'location':'javascript:bad.exe'}))
        self.assertIsNone(r.file_target({**observation,'location':'http://user:pass@dl.mirror.test/trial.exe'}))
        self.assertIsNone(r.file_target({**observation,'status':200}))
        self.assertIsNone(r.file_target({**observation,'location':'http://dl.mirror.test/login.php'}))

    def test_recovery_adds_original_url_and_wakes_failed_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                record=a.parse_detail(HTML,'https://example.test/items/1','d_station')[0]
                record['mirrors']=[self.target];store.add(record);store.update(record['id'],status='failed')
                r.initialize(store)
                observations=[{'url':self.capture,'checked_at':a.now(),'status':302,'location':a.replay(self.binary,'20070102030405',raw=True,scheme='http'),'response_url':self.capture}]
                self.assertEqual(r.add_observations(store,record['id'],record,observations),1)
                row=store.rows()[0];data=json.loads(row['data'])
                self.assertEqual(data['mirrors'],[self.target,self.binary])
                self.assertEqual(data['expected_md5'],record['expected_md5'])
                self.assertEqual(row['status'],'pending')
                self.assertEqual(store.db.execute('SELECT count(*) FROM wayback_candidates').fetchone()[0],0)
                self.assertEqual(r.add_observations(store,record['id'],record,observations),0)
                self.assertEqual(len(json.loads(store.rows()[0]['data'])['fields']['redirect_evidence']),1)
                changed={**record,'expected_md5':'f'*32}
                self.assertEqual(r.add_observations(store,record['id'],changed,observations),0)
            finally:store.db.close()

    def test_http_error_redirect_headers_are_cached_and_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                r.initialize(store);headers=Message();headers['Location']=a.replay(self.binary,'20070102030405',raw=True,scheme='http')
                error=HTTPError(self.capture,302,'Found',headers,None)
                with patch.object(a,'open_archive_page',side_effect=error) as opened:
                    first=r.observe(store,Fetch([]),self.capture)
                    second=r.observe(store,Fetch([]),self.capture)
                self.assertEqual(first,second);self.assertEqual(first['status'],302);opened.assert_called_once()
            finally:store.db.close()

    def test_completed_empty_lookup_does_not_starve_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                for number in range(2):
                    record=a.parse_detail(HTML,'https://example.test/items/'+str(number),'d_station')[0]
                    record['mirrors']=[self.target+str(number)];store.add(record);store.update(record['id'],status='failed')
                with patch.object(r,'lookup',return_value=[]) as lookup:
                    self.assertEqual(r.collect(store,Fetch([]),Fetch([]),limit=1),(1,0,0))
                    self.assertEqual(r.collect(store,Fetch([]),Fetch([]),limit=1),(1,0,0))
                    self.assertEqual(r.collect(store,Fetch([]),Fetch([]),limit=1),(0,0,0))
                    self.assertEqual(lookup.call_count,2)
            finally:store.db.close()

    def test_follows_only_returned_replay_locations_and_bounds_cycles(self):
        next_url='https://web.archive.org/web/20070203040506id_/https://mirror.test/download.php?id=1'
        first={'url':self.capture,'status':301,'location':next_url,'response_url':self.capture}
        final={'url':next_url,'status':302,'location':a.replay(self.binary,'20070203040506',raw=True),'response_url':next_url}
        with patch.object(r,'observe',side_effect=[first,final]) as observe:
            self.assertEqual(list(r.follow(None,None,self.capture)),[first,final])
            self.assertEqual(observe.call_args_list[1].args[2],next_url)
        with patch.object(r,'observe',return_value={**first,'location':self.capture}):
            with self.assertRaisesRegex(ValueError,'cycle'):list(r.follow(None,None,self.capture))
        with patch.object(r,'observe',return_value={**first,'location':'https://unrelated.test/login'}) as observe:
            self.assertEqual(list(r.follow(None,None,self.capture)),[{**first,'location':'https://unrelated.test/login'}])
            observe.assert_called_once()
