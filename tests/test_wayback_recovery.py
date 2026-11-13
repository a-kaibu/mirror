import json
import tempfile
import unittest
from unittest.mock import Mock, patch
import archiver as a
from wayback_recovery import captures, discover
from test_archiver import Response, Fetch, HTML

TARGET = "http://mirror.test/demo.zip"
ROWS = [["timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ["20080102030405", TARGET, "application/zip", "200", "archive-sha1", "99"],
    ["20181120233710", TARGET, "application/zip", "200", "archive-sha1", "99"]]

class WaybackRecoveryTests(unittest.TestCase):
    def test_limited_pass_queries_unseen_then_oldest_failed_urls(self):
        from urllib.error import URLError
        from urllib.parse import parse_qs,urlsplit
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                record=a.parse_detail(HTML,'https://source.test/item','kokoron')[0]
                record['mirrors']=['http://mirror.test/a.zip','http://mirror.test/b.zip']
                store.add(record);store.update(record['id'],status='failed')
                for stamp,expected in [('2020-01-01T00:00:00+00:00','a.zip'),('2020-01-02T00:00:00+00:00','b.zip'),('2020-01-03T00:00:00+00:00','a.zip')]:
                    fetch=Fetch([URLError('unavailable')])
                    with patch('wayback_recovery.a.now',return_value=stamp):
                        self.assertEqual(discover(store,fetch,limit=1),(1,0))
                    target=parse_qs(urlsplit(fetch.requests[0][0]).query)['url'][0]
                    self.assertTrue(target.endswith(expected))
            finally:store.db.close()

    def test_cached_target_shared_by_items_uses_no_extra_network_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                for i in range(2):
                    record=a.parse_detail(HTML,'https://source.test/'+str(i),'kokoron')[0]
                    record['mirrors']=[TARGET];store.add(record);store.update(record['id'],status='failed')
                fetch=Fetch([Response(json.dumps(ROWS).encode())])
                self.assertEqual(discover(store,fetch,limit=1),(1,4))
                self.assertEqual(len(fetch.requests),1)
                self.assertEqual(store.db.execute('SELECT count(*) FROM wayback_candidates').fetchone()[0],4)
            finally:store.db.close()

    def test_keeps_distinct_dates_even_when_index_digest_matches(self):
        result = captures(ROWS, TARGET)
        self.assertEqual(len(result), 2)
        self.assertIn("20080102030405id_", result[0]["url"])
        self.assertIn("20181120233710id_", result[1]["url"])
        self.assertEqual(captures([], TARGET), [])

    def test_default_port_spellings_match_but_other_services_do_not(self):
        for original in ['http://mirror.test:80/demo.zip','https://mirror.test:443/demo.zip']:
            row=ROWS[1].copy();row[1]=original
            accepted=captures([ROWS[0],row],TARGET)
            self.assertEqual(len(accepted),1)
            self.assertEqual(accepted[0]['original_url'],original)
        for original in ['http://mirror.test:8000/demo.zip','https://mirror.test:80/demo.zip',
                         'http://user@mirror.test/demo.zip','http://mirror.test/demo.zip?other=1']:
            row=ROWS[1].copy();row[1]=original
            self.assertEqual(captures([ROWS[0],row],TARGET),[])
        row=ROWS[1].copy();row[1]='http://mirror.test:8000/demo.zip'
        self.assertEqual(len(captures([ROWS[0],row],'http://mirror.test:8000/demo.zip')),1)

    def test_rejects_other_files_html_and_failures(self):
        for index, value in [(1,"http://evil.test/demo.zip"), (1,"http://mirror.test/other.zip"), (2,"text/html"), (3,"404"), (0,"yesterday")]:
            row = ROWS[1].copy(); row[index] = value
            self.assertEqual(captures([ROWS[0], row], TARGET), [])
        with self.assertRaises(ValueError): captures({}, TARGET)
        with self.assertRaises(ValueError): captures([["timestamp"]], TARGET)

    def test_persists_evidence_reuses_index_and_downloads_alternate_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = a.Store(tmp)
            record = a.parse_detail(HTML, "https://example.test/items/1", "lathercraft")[0]
            record.update(site="multi_inside", mirrors=[], fields={"recovery_urls":[TARGET]})
            store.add(record);store.update(record["id"],status="failed")
            network = Fetch([Response(json.dumps(ROWS).encode())])
            self.assertEqual(discover(store, Mock(), site="holyseal"), (0, 0))
            self.assertEqual(discover(store, network), (1, 2))
            # Discovery now wakes this item. Simulate a subsequent failed
            # transfer before checking that the next discovery reuses CDX.
            store.update(record['id'],status='failed')
            never = Mock()
            self.assertEqual(discover(store, never), (0, 2)); never.open.assert_not_called()
            self.assertEqual(store.db.execute("SELECT count(*) FROM wayback_candidates").fetchone()[0], 2)
            self.assertEqual(store.db.execute("SELECT status FROM wayback_lookups").fetchone()[0], "ok")
            from test_archiver import PAYLOAD
            download = Fetch([Response(PAYLOAD)])
            self.assertEqual(a.download(store, download), 0)
            self.assertIn("20181120233710id_", download.requests[0][0])
            store.db.close()

    def test_new_capture_wakes_failed_item_without_repeating_attempted_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                record=a.parse_detail(HTML,'https://example.test/items/1','lathercraft')[0]
                record['mirrors']=[TARGET];store.add(record);store.update(record['id'],status='failed')
                discover(store,Fetch([Response(json.dumps(ROWS).encode())]))
                self.assertEqual(store.rows()[0]['status'],'pending')
                for candidate in store.db.execute('select url from wayback_candidates').fetchall():
                    store.db.execute('insert into attempts(item_id,url,attempted_at,error) values(?,?,?,?)',(record['id'],candidate[0],a.now(),'HTTP Error 404'))
                store.db.commit();store.update(record['id'],status='failed')
                discover(store,Mock())
                self.assertEqual(store.rows()[0]['status'],'failed')
            finally:store.db.close()

    def test_actual_public_index_transport_survives_cached_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            record=a.parse_detail(HTML,"https://example.test/items/1","lathercraft")[0]
            record.update(site="multi_inside",mirrors=[TARGET],expected_md5=None)
            store.add(record);store.update(record['id'],status='failed')
            index='http://web.archive.org/cdx/search/cdx?url=http%3A%2F%2Fmirror.test%2Fdemo.zip'
            fetch=Fetch([Response(json.dumps(ROWS).encode(),url=index)])
            self.assertEqual(discover(store,fetch),(1,2))
            urls=[r[0] for r in store.db.execute('SELECT url FROM wayback_candidates')]
            self.assertTrue(all(u.startswith('http://web.archive.org/web/') for u in urls))
            store.update(record['id'],status='failed')
            never=Mock();self.assertEqual(discover(store,never),(0,2));never.open.assert_not_called()
            self.assertEqual(store.db.execute('SELECT index_url FROM archive_index_transports WHERE target_url=?',(TARGET,)).fetchone()[0],index)
            store.db.close()
