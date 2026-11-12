from contextlib import closing
from html import escape
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import erogamescape_full as f


def html(rows):
    return ("<table><tr><th>metadata</th></tr>"+"".join("<tr><td>"+escape(json.dumps(row,ensure_ascii=False))+"</td></tr>" for row in rows)+"</table>").encode()
def count(n):return html([{"id":1,"count":n}])
def records(rows):return html([{"id":1,"row":row} for row in rows])
def column(name,kind="text"):return {"name":name,"type":kind,"udt":kind,"nullable":"NO"}

class FullExportTests(unittest.TestCase):
    def test_service_credentials_are_excluded_before_data_queries(self):
        for name,cols in [("users",[column("id","integer")]),("personal_access_tokens",[column("token")]),("other",[column("password")])]:
            self.assertIsNotNone(f.exclusion({"table_name":name,"columns":cols}))
        self.assertIsNone(f.exclusion({"table_name":"userreview","columns":[column("possession","boolean")]}))

    def test_composite_key_resumes_without_losing_rows(self):
        db=sqlite3.connect(":memory:");f.initialize(db)
        table={"table_name":"links","columns":[column("game","integer"),column("uid")],"primary_key":["game","uid"]}
        replies=iter([count(2),records([{"game":1,"uid":"a'\\b"}]),records([{"game":2,"uid":"z"}]),records([]),count(2)])
        queries=[]
        def request(sql):queries.append(sql);return next(replies)
        f.capture(db,table,request,delay=0,max_pages=1)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"running")
        f.capture(db,table,request,delay=0)
        self.assertIn('WHERE ("game","uid") > (1,',queries[2])
        self.assertEqual(db.execute("SELECT count(*) FROM links").fetchone()[0],2)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"complete")
        db.close()

    def test_narrow_pages_resume_without_skipping_when_server_returns_fewer_rows(self):
        import re
        db=sqlite3.connect(':memory:');f.initialize(db)
        table={'table_name':'narrowstats','columns':[column('id','integer')],'primary_key':['id']}
        limits=[]
        def request(sql):
            if 'count(*)' in sql.lower():return count(6001)
            limits.append(int(re.search(r'LIMIT (\d+)',sql)[1]))
            match=re.search(r'\("id"\) > \((\d+)\)',sql);cursor=int(match[1]) if match else 0
            return records([{'id':i} for i in range(cursor+1,min(cursor+1000,6001)+1)])
        try:
            f.capture(db,table,request,delay=0,max_pages=1)
            self.assertEqual(db.execute('SELECT count(*) FROM narrowstats').fetchone()[0],1000)
            f.capture(db,table,request,delay=0)
            self.assertEqual(set(limits),{5000})
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM narrowstats ORDER BY id')],list(range(1,6002)))
            self.assertEqual(db.execute('SELECT status,source_count FROM _capture_state').fetchone()[:],('complete',6001))
        finally:db.close()

    def test_text_numeric_and_wide_rows_keep_conservative_pages(self):
        import re
        variants=[[column('id','integer'),column('text')],
                  [column('id','integer'),column('amount','numeric')],
                  [column('id','integer')]+[column('field'+str(i),'integer') for i in range(16)]]
        for columns in variants:
            with self.subTest(columns=len(columns)):
                db=sqlite3.connect(':memory:');f.initialize(db);limits=[]
                table={'table_name':'widestats','columns':columns,'primary_key':['id']}
                row={c['name']:1 for c in columns}
                def request(sql):
                    if 'count(*)' in sql.lower():return count(1)
                    limits.append(int(re.search(r'LIMIT (\d+)',sql)[1]))
                    return records([] if ' > ' in sql else [row])
                try:
                    f.capture(db,table,request,delay=0)
                    self.assertEqual(set(limits),{500})
                    self.assertEqual(db.execute('SELECT status FROM _capture_state').fetchone()[0],'complete')
                finally:db.close()

    def test_no_primary_key_preserves_identical_rows_and_offset(self):
        db=sqlite3.connect(":memory:");f.initialize(db)
        table={"table_name":"stats","columns":[column("value")],"primary_key":None}
        replies=iter([count(2),records([{"value":"same"},{"value":"same"}]),records([]),count(2)])
        queries=[]
        def request(sql):queries.append(sql);return next(replies)
        f.capture(db,table,request,delay=0)
        self.assertIn("OFFSET 2",queries[2]);self.assertEqual(db.execute("SELECT count(*) FROM stats").fetchone()[0],2)
        db.close()

    def test_exact_decimal_lexeme_and_all_columns_survive(self):
        raw=html([{"id":1,"row":{"id":1,"amount":"DECIMAL"}}]).decode().replace('&quot;DECIMAL&quot;','1.234567890123456789')
        result,_=f.transport("query",lambda _:raw.encode())
        self.assertEqual(result[0]["amount"],"1.234567890123456789")
        self.assertEqual(f.sqlite_type(column("amount","numeric")),"TEXT")

    def test_schema_change_and_source_count_drift_are_not_success(self):
        for replies in [[count(1),records([{"id":1,"new":"unexpected"}])],[count(1),records([{"id":1}]),records([]),count(2)]]:
            db=sqlite3.connect(":memory:");f.initialize(db)
            table={"table_name":"games","columns":[column("id","integer")],"primary_key":["id"]}
            responses=iter(replies)
            f.capture(db,table,lambda _:next(responses),delay=0)
            self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"failed")
            db.close()

    def test_incomplete_database_is_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"full.sqlite3";db=sqlite3.connect(path);f.initialize(db)
            db.execute("INSERT INTO _source_schema VALUES(?,?,NULL)",("games","{}"));db.commit();db.close()
            with patch.object(f.e,"upload_snapshot") as upload:
                with self.assertRaisesRegex(ValueError,"incomplete"):f.publish(path)
                upload.assert_not_called()

    def test_private_review_filter_matches_count_and_page_scope(self):
        table={"table_name":"userreview","columns":[column("uid")],"primary_key":["uid"]}
        self.assertIn("review_private IS TRUE",f.public_filter(table))

    def test_completed_database_is_checkpointed_and_published_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"full.sqlite3";db=sqlite3.connect(path);f.initialize(db)
            schema={"table_name":"games","columns":[column("id","integer")],"primary_key":["id"]}
            db.execute("INSERT INTO _source_schema VALUES(?,?,NULL)",("games",json.dumps(schema)))
            db.execute("CREATE TABLE games(id INTEGER PRIMARY KEY)");db.execute("INSERT INTO games VALUES(1)")
            db.execute("INSERT INTO _capture_state(name,status,rows,source_count) VALUES('games','complete',1,1)");db.commit();db.close()
            def upload(file):
                copied=Path(tmp)/"readback.sqlite3";copied.write_bytes(file.read_bytes())
                with closing(sqlite3.connect(copied)) as actual:
                    self.assertEqual(actual.execute("PRAGMA integrity_check").fetchone()[0],"ok")
                    self.assertEqual(actual.execute("SELECT count(*) FROM games").fetchone()[0],1)
            with patch.object(f.e,"upload_snapshot",side_effect=upload) as send:
                f.publish(path);send.assert_called_once()

    def test_count_failure_is_recorded_for_resumption(self):
        db=sqlite3.connect(":memory:");f.initialize(db)
        schema={"table_name":"games","columns":[column("id","integer")],"primary_key":["id"]}
        f.capture(db,schema,lambda _: (_ for _ in ()).throw(OSError("temporary failure")),delay=0)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"failed")
        replies=iter([count(0),records([]),count(0)])
        f.capture(db,schema,lambda _:next(replies),delay=0)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"complete")
        db.close()
