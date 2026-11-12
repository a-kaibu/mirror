import json
import tempfile
import unittest
from unittest.mock import Mock
import archiver as a
from wayback_recovery import captures, discover
from test_archiver import Response, Fetch, HTML

TARGET = "http://mirror.test/demo.zip"
ROWS = [["timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ["20080102030405", TARGET, "application/zip", "200", "archive-sha1", "99"],
    ["20181120233710", TARGET, "application/zip", "200", "archive-sha1", "99"]]

class WaybackRecoveryTests(unittest.TestCase):
    def test_keeps_distinct_dates_even_when_index_digest_matches(self):
        result = captures(ROWS, TARGET)
        self.assertEqual(len(result), 2)
        self.assertIn("20080102030405id_", result[0]["url"])
        self.assertIn("20181120233710id_", result[1]["url"])
        self.assertEqual(captures([], TARGET), [])

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
            record.update(site="multi_inside", mirrors=[TARGET])
            store.add(record);store.update(record["id"],status="failed")
            network = Fetch([Response(json.dumps(ROWS).encode())])
            self.assertEqual(discover(store, Mock(), site="holyseal"), (0, 0))
            self.assertEqual(discover(store, network), (1, 2))
            never = Mock()
            self.assertEqual(discover(store, never), (0, 2)); never.open.assert_not_called()
            self.assertEqual(store.db.execute("SELECT count(*) FROM wayback_candidates").fetchone()[0], 2)
            self.assertEqual(store.db.execute("SELECT status FROM wayback_lookups").fetchone()[0], "ok")
            from test_archiver import PAYLOAD
            download = Fetch([Response(PAYLOAD)])
            self.assertEqual(a.download(store, download), 0)
            self.assertIn("20181120233710id_", download.requests[0][0])
            store.db.close()
