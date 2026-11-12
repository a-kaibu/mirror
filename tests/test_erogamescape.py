from contextlib import closing
import json
from html import escape
import tempfile
import unittest
import archiver as a
import erogamescape as e
from test_archiver import HTML

def response(rows):
    return ("<table><tr><th>metadata</th></tr>" + "".join("<tr><td>" + escape(json.dumps(row,ensure_ascii=False)) + "</td></tr>" for row in rows) + "</table>").encode()

class ErogameScapeTests(unittest.TestCase):
    def test_parse_preserves_unicode_and_null(self):
        rows=[{"id":1,"brandname":"日本語 & \"quoted\"","url":None}]
        self.assertEqual(e.parse_rows(response(rows).decode()),rows)
        self.assertEqual(e.parse_rows(response([]).decode()),[])
        with self.assertRaises(ValueError):e.parse_rows("<p>SQL error</p>")
        empty = '<div id="queryplan_main">Index Scan</div><div id="query_result_main"></div>'
        self.assertEqual(e.parse_rows(empty), [])
        with self.assertRaises(ValueError):e.parse_rows(empty + '<p>ERROR: timeout</p>')

    def test_keyset_resume_requires_empty_page_for_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            rows=[{"id":2,"brandname":"Brand","url":"https://official.test/"}]
            requests=[]
            def first(sql):requests.append(sql);return response(rows)
            self.assertEqual(e.collect(store,"brand",first,max_pages=1,delay=0),1)
            self.assertEqual(store.db.execute("SELECT complete FROM metadata_sync").fetchone()[0],0)
            def last(sql):requests.append(sql);return response([])
            e.collect(store,"brand",last,delay=0)
            self.assertIn("id>2",requests[-1])
            self.assertEqual(store.db.execute("SELECT complete FROM metadata_sync").fetchone()[0],1)
            self.assertEqual(store.db.execute("SELECT count(*) FROM external_metadata").fetchone()[0],1)
            store.db.close()

    def test_failed_import_retains_cursor_and_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            with self.assertRaises(ValueError):e.collect(store,"brand",lambda sql:b"<p>error</p>",delay=0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM external_metadata").fetchone()[0],0)
            self.assertIsNotNone(store.db.execute("SELECT error FROM metadata_fetches").fetchone()[0])
            store.db.close()

    def test_brand_and_title_match_does_not_fuzzy_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);e.initialize(store)
            record=a.parse_detail(HTML,"https://example.test/items/1","lathercraft")[0]
            record.update(brand="Brand",title="作品")
            store.add(record)
            for entity,rows in {"brand":[{"id":1,"brandname":"Ｂｒａｎｄ","url":"https://official.test/"}],"game":[{"id":2,"gamename":"作品","brandname":1,"trial_url":"https://official.test/trial.html"}]}.items():
                for row in rows:store.db.execute("INSERT INTO external_metadata VALUES(?,?,?,?,?)",("erogamescape",entity,row["id"],json.dumps(row),a.now()))
            store.db.commit()
            self.assertEqual(e.match(store),[("brand",1),("game",1)])
            manifest=store.export()
            self.assertEqual(manifest["manufacturers"][0]["erogamescape"][0]["id"],1)
            self.assertEqual(manifest["products"][0]["erogamescape"][0]["data"]["trial_url"],"https://official.test/trial.html")
            self.assertEqual(store.db.execute("SELECT method FROM metadata_links WHERE entity='game'").fetchone()[0],"brand_and_title")
            store.db.close()

    def test_export_refuses_incomplete_import_and_checks_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);e.initialize(store)
            with self.assertRaises(ValueError):e.export_sqlite(store)
            data={"brand":[{"id":1,"brandname":"Brand","url":"https://official.test/"}],"game":[{"id":2,"brandname":1,"gamename":"作品","trial_url":None}],"holyseal":[{"id":3,"game":2,"holyseal_id":55}]}
            for entity,rows in data.items():
                batch=iter([response(rows),response([])])
                e.collect(store,entity,lambda sql:next(batch),delay=0)
            expected={"id":1,"brand":1,"game":1,"holyseal":1}
            self.assertEqual(e.verify_counts(store,lambda sql:response([expected])),expected)
            with self.assertRaises(ValueError):e.verify_counts(store,lambda sql:response([{**expected,"game":2}]))
            import sqlite3
            path=e.export_sqlite(store)
            with closing(sqlite3.connect(path)) as snapshot:
                self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0],"ok")
                self.assertEqual(snapshot.execute("SELECT gamename FROM gamelist WHERE id=2").fetchone()[0],"作品")
                self.assertEqual(snapshot.execute("SELECT count(*) FROM provenance").fetchone()[0],3)
            store.db.close()
