import html
import json
import re
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch
import erogamescape_full as f
import erogamescape_timestamp as t


def response(rows):
    return ('<table><tr><th>metadata</th></tr>'+''.join('<tr><td>'+html.escape(json.dumps(r))+'</td></tr>' for r in rows)+'</table>').encode()


class Source:
    def __init__(self, rows):
        self.rows=rows;self.fail_page=None;self.pages=0;self.cost_above=None;self.drift=False
    def __call__(self, sql):
        if ' >= ' not in sql:
            if ' < ' in sql:
                limit=re.search(r" < E'([^']+)'",sql)[1]
                rows=[r for r in self.rows if datetime.fromisoformat(r['timestamp']) < datetime.fromisoformat(limit)]
            else:
                return '<div>エラー(実行コスト高)</div>'.encode()
        elif ' AND ' not in sql:
            limit=re.search(r" >= E'([^']+)'",sql)[1]
            rows=[r for r in self.rows if datetime.fromisoformat(r['timestamp']) >= datetime.fromisoformat(limit)]
        else:
            lo,hi=re.findall(r"(?:>=|<) E'([^']+)'",sql)
            rows=[r for r in self.rows if datetime.fromisoformat(lo)<=datetime.fromisoformat(r['timestamp'])<datetime.fromisoformat(hi)]
        if "'count'" in sql:
            if self.cost_above is not None and len(rows)>self.cost_above:
                return '<div>エラー(実行コスト高)</div>'.encode()
            return response([{'id':1,'count':len(rows)+(1 if self.drift and ' AND ' in sql else 0)}])
        self.pages+=1
        if self.fail_page==self.pages:raise OSError('interrupted')
        offset=int(re.search(r'OFFSET (\d+)',sql)[1]);limit=int(re.search(r'LIMIT (\d+)',sql)[1])
        rows=sorted(rows,key=lambda r:json.dumps(r,sort_keys=True))[offset:offset+limit]
        return response([{'id':1,'row':r} for r in rows])


class TimestampTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');f.initialize(self.db)
        self.table={'table_name':'history','primary_key':None,'columns':[
            {'name':'word','type':'text','nullable':'YES'},
            {'name':'timestamp','type':'timestamp without time zone','nullable':'NO'}]}
        self.db.execute('CREATE TABLE history(word TEXT,timestamp TEXT)')
        self.db.execute("INSERT INTO _capture_state(name,status) VALUES('history','failed')");self.db.commit()
    def tearDown(self):self.db.close()
    def rows(self,n=5):return [{'word':'same','timestamp':'2000-01-01T00:00:00'} for _ in range(n)]
    def state(self):return self.db.execute("SELECT * FROM _capture_state WHERE name='history'").fetchone()
    @patch.object(t,'LOWER','2000-01-01T00:00:00')
    @patch.object(t,'UPPER','2000-01-01T00:00:00.000001')
    def test_duplicate_rows_and_dense_single_microsecond_are_preserved(self):
        with patch.object(t,'BATCH',2):t.capture(self.db,self.table,Source(self.rows()),delay=0)
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['source_count'],5)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],5)
        self.assertEqual(self.db.execute('SELECT count(*) FROM _timestamp_pending').fetchone()[0],0)
    @patch.object(t,'LOWER','2000-01-01T00:00:00')
    @patch.object(t,'UPPER','2000-01-01T00:00:00.000001')
    def test_interruption_resumes_staging_without_duplicate_rows(self):
        remote=Source(self.rows());remote.fail_page=2
        with patch.object(t,'BATCH',2):
            t.capture(self.db,self.table,remote,delay=0)
            self.assertEqual(self.state()['status'],'failed')
            self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],0)
            self.assertEqual(self.db.execute('SELECT count(*) FROM _timestamp_pending').fetchone()[0],2)
            remote.fail_page=None;t.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],5)
    @patch.object(t,'LOWER','2000-01-01T00:00:00')
    @patch.object(t,'UPPER','2000-01-01T00:00:00.000001')
    def test_bounded_run_resumes_through_normal_capture_dispatch(self):
        remote=Source(self.rows(5))
        with patch.object(t,'BATCH',2):
            t.capture(self.db,self.table,remote,delay=0,max_pages=1)
        self.assertEqual(self.state()['status'],'running')
        f.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['rows'],5)
    def test_count_cost_splits_instead_of_omitting_rows(self):
        remote=Source([{'word':str(i),'timestamp':f'200{i}-01-01T00:00:00'} for i in range(4)])
        remote.cost_above=2
        t.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['rows'],4)
    def test_finite_domain_is_verified_and_outliers_fail(self):
        t.capture(self.db,self.table,Source([{'word':'old','timestamp':'1800-01-01T00:00:00'}]),delay=0)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('outside verified',self.state()['error'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],0)
    def test_unpartitioned_rows_are_never_silently_appended(self):
        self.db.execute("INSERT INTO history VALUES('old','2000-01-01')");self.db.commit()
        t.capture(self.db,self.table,Source(self.rows()),delay=0)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('reconciliation',self.state()['error'])
    def test_count_drift_never_marks_complete(self):
        remote=Source(self.rows(1));remote.drift=True
        t.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('count drift',self.state()['error'])
    def test_global_cost_fallback_uses_timestamp_partitions(self):
        remote=Source(self.rows(1));f.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['rows'],1)
    def test_complete_export_is_idempotent(self):
        remote=Source(self.rows(3));t.capture(self.db,self.table,remote,delay=0)
        pages=remote.pages;t.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(remote.pages,pages)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],3)
    def test_final_recount_detects_changed_completed_partition(self):
        remote=Source(self.rows(1));calls=0
        def request(sql):
            nonlocal calls
            if "'count'" in sql and ' AND ' in sql:
                calls+=1
                if calls==3:remote.rows.append({'word':'added','timestamp':'2000-01-01T00:00:00'})
            return remote(sql)
        t.capture(self.db,self.table,request,delay=0)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('source count drift',self.state()['error'])
    def test_partition_gap_cannot_be_marked_complete(self):
        remote=Source(self.rows(1));t.capture(self.db,self.table,remote,delay=0,max_pages=1)
        self.db.execute("UPDATE _capture_state SET status='running'")
        self.db.execute("UPDATE _timestamp_ranges SET upper='2099-01-01T00:00:00'")
        self.db.commit();t.capture(self.db,self.table,remote,delay=0)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('source domain changed',self.state()['error'])
    @patch.object(t,'LOWER','2000-01-01T00:00:00')
    @patch.object(t,'UPPER','2000-01-01T00:00:00.000001')
    def test_failed_leaf_never_exposes_partially_staged_output(self):
        remote=Source(self.rows(3))
        with patch.object(t,'BATCH',2):
            t.capture(self.db,self.table,remote,delay=0,max_pages=1)
            self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],0)
            self.assertEqual(self.db.execute('SELECT count(*) FROM _timestamp_pending').fetchone()[0],2)
    def test_visibility_filter_is_never_dropped(self):
        table=dict(self.table,table_name='userreview_history')
        table['columns']=self.table['columns']+[{'name':'uid','type':'text','nullable':'NO'}]
        self.db.execute("INSERT INTO _capture_state(name,status) VALUES('userreview_history','failed')")
        self.db.commit();t.capture(self.db,table,lambda sql:self.fail('must not query without visibility predicate'),delay=0)
        row=self.db.execute("SELECT status,error FROM _capture_state WHERE name='userreview_history'").fetchone()
        self.assertEqual(row['status'],'failed')
        self.assertIn('visibility-filtered',row['error'])
    @patch.object(t,'LOWER','2000-01-01T00:00:00')
    @patch.object(t,'UPPER','2000-01-01T00:00:00.000001')
    def test_oversized_pages_shrink_at_saved_offset_without_losing_duplicates(self):
        remote=Source(self.rows(7));attempts=[]
        def request(sql):
            if 'OFFSET' in sql:
                limit=int(re.search(r'LIMIT (\d+)',sql)[1]);offset=int(re.search(r'OFFSET (\d+)',sql)[1])
                attempts.append((limit,offset))
                if limit>2:raise ValueError('SQL response exceeds 16 MiB')
            return remote(sql)
        with patch.object(t,'BATCH',8):t.capture(self.db,self.table,request,delay=0,max_pages=1)
        self.assertEqual(attempts,[(8,0),(4,0),(2,0)])
        leaf=self.db.execute("SELECT page_size,fetched FROM _timestamp_ranges WHERE status='pending'").fetchone()
        self.assertEqual(tuple(leaf),(2,2))
        with patch.object(t,'BATCH',8):t.capture(self.db,self.table,request,delay=0)
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.state()['rows'],7)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],7)
    def test_single_row_over_transport_limit_fails_without_completion(self):
        remote=Source(self.rows(1))
        def request(sql):
            if 'OFFSET' in sql:raise ValueError('SQL response exceeds 16 MiB')
            return remote(sql)
        with patch.object(t,'BATCH',1):t.capture(self.db,self.table,request,delay=0)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('exceeds 16 MiB',self.state()['error'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM history').fetchone()[0],0)
    def test_existing_partition_ledger_upgrades_without_replacing_progress(self):
        self.db.execute('CREATE TABLE _timestamp_ranges(name TEXT,lower TEXT,upper TEXT,status TEXT,fetched INTEGER,source_count INTEGER,PRIMARY KEY(name,lower,upper))')
        self.db.execute("INSERT INTO _timestamp_ranges VALUES('history','a','b','pending',123,500)");self.db.commit()
        t.initialize(self.db)
        row=self.db.execute('SELECT fetched,source_count,page_size FROM _timestamp_ranges').fetchone()
        self.assertEqual(tuple(row),(123,500,5000))
    def test_unsupported_timestamp_key_is_rejected(self):
        table=dict(self.table,primary_key=['word'])
        with self.assertRaisesRegex(ValueError,'without a primary key'):t.timestamp_key(table)


if __name__=='__main__':unittest.main()
