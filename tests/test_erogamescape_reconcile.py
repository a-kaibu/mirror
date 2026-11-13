import json
import sqlite3
import unittest
from test_erogamescape_full import column,count,records
import erogamescape_full as f


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');f.initialize(self.db)
        self.addCleanup(self.db.close)
        self.table={'table_name':'games','columns':[column('id','integer'),column('value')],'primary_key':['id']}
        self.db.execute('CREATE TABLE games(id INTEGER PRIMARY KEY,value TEXT)')
        self.db.executemany('INSERT INTO games VALUES(?,?)',[(2,'old'),(4,'deleted')])
        self.db.execute("INSERT INTO _capture_state(name,status,cursor,offset,rows,error) VALUES('games','failed','[4]',2,2,'count drift: source changed')")
        self.db.commit()

    def capture(self,replies,**kwargs):
        replies=iter(replies);queries=[]
        def request(sql):
            queries.append(sql);result=next(replies)
            if isinstance(result,Exception):raise result
            return result
        f.capture(self.db,self.table,request,delay=0,**kwargs)
        return queries

    def values(self):return [tuple(r) for r in self.db.execute('SELECT * FROM games ORDER BY id')]
    def state(self):return dict(self.db.execute("SELECT * FROM _capture_state WHERE name='games'").fetchone())

    def test_rebuild_finds_early_insert_updates_and_deletions(self):
        new=[{'id':1,'value':'early'},{'id':2,'value':'updated'},{'id':3,'value':'new'}]
        self.capture([count(3),records(new)],max_pages=1)
        self.assertEqual(self.values(),[(2,'old'),(4,'deleted')])
        self.assertEqual(self.state()['status'],'running')
        queries=self.capture([records([]),count(3)])
        self.assertIn('("id") > (3)',queries[0])
        self.assertEqual(self.values(),[(1,'early'),(2,'updated'),(3,'new')])
        self.assertEqual((self.state()['status'],self.state()['rows'],self.state()['source_count']),('complete',3,3))
        receipt=json.loads(self.db.execute("SELECT value FROM _archive_info WHERE key='reconciliation_final:games'").fetchone()[0])
        self.assertEqual(receipt['rows'],3)
        self.assertEqual(self.db.execute('SELECT count(*) FROM _reconcile_state').fetchone()[0],0)

    def test_network_failure_resumes_staging_without_replacing_previous_copy(self):
        self.capture([count(1),records([{'id':1,'value':'new'}]),OSError('connection lost')])
        self.assertEqual(self.state()['status'],'failed')
        self.assertEqual(self.values(),[(2,'old'),(4,'deleted')])
        queries=self.capture([records([]),count(1)])
        self.assertIn('("id") > (1)',queries[0])
        self.assertEqual(self.values(),[(1,'new')])
        self.assertEqual(self.state()['status'],'complete')

    def test_new_count_drift_requires_another_full_pass(self):
        self.capture([count(1),records([{'id':2,'value':'new'}]),records([]),count(2)])
        self.assertEqual(self.state()['status'],'failed')
        self.assertEqual(self.values(),[(2,'old'),(4,'deleted')])
        queries=self.capture([count(2),records([{'id':1,'value':'early'},{'id':2,'value':'new'}]),records([]),count(2)])
        self.assertNotIn(' > ',queries[1])
        self.assertEqual(self.values(),[(1,'early'),(2,'new')])
        receipt=json.loads(self.db.execute("SELECT value FROM _archive_info WHERE key='reconciliation_final:games'").fetchone()[0])
        self.assertEqual(receipt['passes'],2)

    def test_growth_captured_during_pass_can_complete(self):
        self.capture([count(1),records([{'id':1,'value':'first'},{'id':2,'value':'added'}]),records([]),count(2)])
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['source_count'],2)

    def test_composite_text_key_resume_uses_all_dimensions(self):
        self.table={'table_name':'tagged','columns':[column('tag'),column('game','integer'),column('uid')],'primary_key':['tag','game','uid']}
        self.db.execute('CREATE TABLE tagged(tag TEXT,game INTEGER,uid TEXT,PRIMARY KEY(tag,game,uid))')
        self.db.execute("INSERT INTO _capture_state(name,status,error) VALUES('tagged','failed','count drift: changed')")
        self.capture([count(1),records([{'tag':"a'",'game':7,'uid':'user'}])],max_pages=1)
        queries=self.capture([records([]),count(1)])
        self.assertIn('("tag","game","uid") > (E',queries[0])
        self.assertIn("7,E'user'",queries[0])
        self.assertEqual(self.db.execute("SELECT status FROM _capture_state WHERE name='tagged'").fetchone()[0],'complete')

    def test_malformed_rows_do_not_overwrite_previous_copy(self):
        self.capture([count(1),records([{'id':1,'other':'wrong schema'}])])
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('schema changed',self.state()['error'])
        self.assertEqual(self.values(),[(2,'old'),(4,'deleted')])

    def test_reconciliation_keeps_review_visibility_filter(self):
        self.table={'table_name':'userreview_test','columns':[column('id','integer'),column('uid')],'primary_key':['id']}
        self.db.execute('CREATE TABLE userreview_test(id INTEGER PRIMARY KEY,uid TEXT)')
        self.db.execute("INSERT INTO _capture_state(name,status,error) VALUES('userreview_test','failed','count drift: changed')")
        replies=iter([count(1),records([{'id':1,'uid':'public'}]),records([]),count(1)])
        queries=[]
        def request(sql):queries.append(sql);return next(replies)
        f.capture(self.db,self.table,request,delay=0)
        for sql in queries:self.assertIn('visibility.review_private IS TRUE',sql)
        self.assertEqual(self.db.execute("SELECT status FROM _capture_state WHERE name='userreview_test'").fetchone()[0],'complete')


if __name__=='__main__':unittest.main()
