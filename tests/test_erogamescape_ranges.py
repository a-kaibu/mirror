import json
import re
import sqlite3
import unittest
import erogamescape_full as f
import erogamescape_ranges as r
from test_erogamescape_full import html,records,count,column

COST=b'<div id="query_result_main">Error</div><p>'+"エラー(実行コスト高) SQLの実行コストが150000を超えています".encode()+b'</p>'

class Remote:
    def __init__(self,columns,rows,limit=2,collation="C"):
        self.db=sqlite3.connect(":memory:");self.db.row_factory=sqlite3.Row
        self.db.execute("CREATE TABLE source("+",".join(f.quote(c["name"])+" "+f.sqlite_type(c) for c in columns)+")")
        self.db.executemany("INSERT INTO source VALUES("+",".join("?" for _ in columns)+")",rows)
        self.limit=limit;self.collation=collation;self.queries=[];self.fail_next=False
    def __call__(self,sql):
        self.queries.append(sql)
        if "FROM pg_attribute" in sql:return html([{"id":1,"collation":"default","database_collation":self.collation}])
        if "'count',count(*)" in sql:return count(self.db.execute("SELECT count(*) FROM source").fetchone()[0])
        if self.fail_next:
            self.fail_next=False;raise OSError("temporary unavailable")
        inner=re.search(r"FROM \(SELECT src\.\* FROM (.*)\) t$",sql).group(1)
        query="SELECT src.* FROM "+inner
        def unescape(match):
            text=match[1].replace("''","'").replace("\\\\","\\")
            return "'"+text.replace("'","''")+"'"
        query=re.sub(r"E'((?:[^']|'')*)'",unescape,query)
        rows=self.db.execute(query).fetchall()
        if len(rows)>self.limit:return COST
        return records([dict(row) for row in rows])

class RangeExportTests(unittest.TestCase):
    def setup_capture(self,columns,pk,rows,limit=2):
        db=sqlite3.connect(":memory:");f.initialize(db)
        table={"table_name":"source","columns":columns,"primary_key":pk}
        remote=Remote(columns,rows,limit)
        return db,table,remote

    def test_numeric_ranges_include_negative_keys_zero_and_composite_boundaries(self):
        db,table,remote=self.setup_capture([column("game","integer"),column("uid")],["game","uid"],
            [(-3,"a"),(-1,"b"),(0,"c"),(0,"d"),(1,"e"),(100,"f")])
        f.capture(db,table,remote,delay=0)
        self.assertEqual(db.execute("SELECT game,uid FROM source ORDER BY game,uid").fetchall()[0][:],(-3,"a"))
        self.assertEqual(db.execute("SELECT count(*) FROM source").fetchone()[0],6)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"complete")
        self.assertTrue(db.execute("SELECT count(*) FROM _capture_ranges WHERE status='split'").fetchone()[0])
        db.close();remote.db.close()

    def test_text_ranges_preserve_empty_unicode_and_composite_keys(self):
        rows=[("",1),("d_001",1),("d_001",2),("d_002",1),("あ",1),("𐀀",1)]
        db,table,remote=self.setup_capture([column("cid"),column("date","integer")],["cid","date"],rows)
        f.capture(db,table,remote,delay=0)
        self.assertEqual([tuple(v) for v in db.execute("SELECT cid,date FROM source ORDER BY cid,date")],rows)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"complete")
        db.close();remote.db.close()

    def test_interruption_and_transport_failure_resume_ranges_without_duplicates(self):
        rows=[("a",1),("b",1),("c",1),("d",1)]
        db,table,remote=self.setup_capture([column("cid"),column("date","integer")],["cid","date"],rows)
        f.capture(db,table,remote,delay=0,max_pages=1)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"running")
        remote.fail_next=True
        r.capture_ranges(db,table,remote,delay=0)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"failed")
        r.capture_ranges(db,table,remote,delay=0)
        self.assertEqual(db.execute("SELECT count(*) FROM source").fetchone()[0],4)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"complete")
        db.close();remote.db.close()

    def test_nonbinary_text_collation_fails_without_inventing_range_order(self):
        db,table,remote=self.setup_capture([column("cid")],["cid"],[("a",),("b",),("c",)])
        remote.collation="ja_JP.UTF-8"
        f.capture(db,table,remote,delay=0)
        self.assertEqual(db.execute("SELECT status FROM _capture_state").fetchone()[0],"failed")
        self.assertIn("binary text collation",db.execute("SELECT error FROM _capture_state").fetchone()[0])
        self.assertEqual(db.execute("SELECT count(*) FROM source").fetchone()[0],0)
        db.close();remote.db.close()

    def test_excluded_tables_are_rejected_before_any_request(self):
        db=sqlite3.connect(":memory:");f.initialize(db)
        table={"table_name":"users","columns":[column("id","integer")],"primary_key":["id"]}
        with self.assertRaisesRegex(ValueError,"excluded"):
            r.capture_ranges(db,table,lambda _:self.fail("must not request service internals"),delay=0)
        db.close()

    def test_query_cost_and_other_server_failures_are_distinct(self):
        with self.assertRaises(f.QueryCostExceeded):f.transport("test",lambda _:COST)
        with self.assertRaises(ValueError) as caught:f.transport("test",lambda _:b'<p>Temporary server error</p>')
        self.assertNotIsInstance(caught.exception,f.QueryCostExceeded)

    def test_text_split_boundaries_are_strict_and_exclude_surrogates(self):
        for lower,upper in [("","z"),("d_000","d_001"),("d_",None),("あ","𐀀")]:
            pivot=r.split_text(lower,upper)
            self.assertGreater(pivot,lower)
            if upper is not None:self.assertLess(pivot,upper)
            self.assertTrue(all(not 0xD800<=ord(c)<=0xDFFF for c in pivot))
        with self.assertRaises(ValueError):r.split_text("a","a\x01")
