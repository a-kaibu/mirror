import hashlib
import json
import re
import sqlite3
import unittest
from unittest.mock import patch

import erogamescape_full as f
import erogamescape_ranges as r
import erogamescape_repair as repair
from test_erogamescape_full import column,count,records,html
from test_erogamescape_ranges import COST


class Remote:
    def __init__(self,table,rows):
        self.table=table;self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE source(game INTEGER,uid TEXT,value TEXT,flag INTEGER,PRIMARY KEY(game,uid))')
        self.db.executemany('INSERT INTO source VALUES(?,?,?,?)',rows)
        self.db.execute('CREATE TABLE myuserview(uid TEXT,review_private INTEGER)')
        self.queries=[];self.body_requests=0;self.fail_body=None;self.summary_limit=None;self.mutate_body=None;self.final_count_delta=0
    def rows(self,where):
        where=re.sub(r"E'((?:[^']|'')*)'",lambda m:"'"+m[1].replace("\\\\","\\")+"'",where)
        rows=self.db.execute('SELECT * FROM source src WHERE '+where+' ORDER BY game,uid').fetchall()
        return [dict(row,flag=None if row['flag'] is None else bool(row['flag'])) for row in rows]
    def __call__(self,sql):
        self.queries.append(sql)
        if 'FROM pg_attribute' in sql:return html([{'id':1,'collation':'C'}])
        if "'count',count(*)" in sql:
            predicate=sql.split(' WHERE ',1)[1] if ' WHERE ' in sql else 'TRUE'
            return count(len(self.rows(predicate))+self.final_count_delta)
        if 'string_agg' in sql:
            where=sql.split(' src WHERE ',1)[1].split(' GROUP BY ',1)[0]
            grouped={}
            for row in self.rows(where):grouped.setdefault(row['game'],[]).append(row)
            if self.summary_limit is not None and len(grouped)>self.summary_limit:return COST
            groups=[]
            for key,items in sorted(grouped.items()):
                digest=hashlib.md5()
                for item in items:
                    row=json.dumps([item[c['name']] for c in self.table['columns']],ensure_ascii=False,separators=(', ', ': '))
                    digest.update(hashlib.md5(row.encode()).hexdigest().encode())
                groups.append({'group_key':key,'rows':len(items),'digest':digest.hexdigest()})
            return records(groups[:257])
        self.body_requests+=1
        if self.fail_body==self.body_requests:raise OSError('lost response')
        if self.mutate_body:
            callback=self.mutate_body;self.mutate_body=None;callback()
        where=sql.split(' src WHERE ',1)[1].rsplit(' ORDER BY ',1)[0]
        limit=int(re.search(r' LIMIT ([0-9]+)\) t$',sql)[1])
        return records(self.rows(where)[:limit])


class GroupRepairTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');f.initialize(self.db);r.initialize(self.db);self.addCleanup(self.db.close)
        self.table={'table_name':'source','columns':[column('game','integer'),column('uid'),column('value'),column('flag','boolean')],'primary_key':['game','uid']}
        self.db.execute('CREATE TABLE source(game INTEGER,uid TEXT,value TEXT,flag INTEGER,PRIMARY KEY(game,uid))')
        self.db.executemany('INSERT INTO source VALUES(?,?,?,?)',[(2,'u','keep',1),(3,'u','old',0),(4,'u','removed',None)])
        self.db.execute("INSERT INTO _capture_state(name,status,rows,offset,error) VALUES('source','failed',3,3,'count drift: changed')")
        self.db.execute("INSERT INTO _range_plans VALUES('source','game','integer',NULL,'earlier')")
        self.db.execute("INSERT INTO _capture_ranges VALUES('source','','-2147483648','2147483648','complete','earlier',NULL)")
        self.db.commit()
        self.remote=Remote(self.table,[(1,'u','early',1),(2,'u','keep',1),(3,'u','new',0)])
        self.addCleanup(self.remote.db.close)
    def run_repair(self,**kwargs):
        def request(sql):
            self.assertFalse(self.db.in_transaction,'network request held SQLite transaction')
            return self.remote(sql)
        r.capture_ranges(self.db,self.table,request,delay=0,**kwargs)
    def state(self):return dict(self.db.execute("SELECT * FROM _capture_state WHERE name='source'").fetchone())
    def values(self):return [tuple(row) for row in self.db.execute('SELECT * FROM source ORDER BY game,uid')]

    def test_range_retry_repairs_early_insert_equal_count_update_and_deletion(self):
        self.run_repair()
        self.assertEqual(self.values(),[(1,'u','early',1),(2,'u','keep',1),(3,'u','new',0)])
        self.assertEqual(self.state()['status'],'complete');self.assertEqual(self.state()['source_count'],3)
        bodies=[q for q in self.remote.queries if 'SELECT src.*' in q]
        self.assertFalse(any('"game" = 2' in q for q in bodies))
        receipt=json.loads(self.db.execute("SELECT value FROM _archive_info WHERE key='reconciliation_final:source'").fetchone()[0])
        self.assertEqual(receipt['method'],'ordered full-row group fingerprints')
        self.assertFalse(repair.active(self.db,'source'))

    def test_network_failure_retains_old_group_and_resumes_staged_cursor(self):
        self.remote.db.execute("INSERT INTO source VALUES(1,'v','second',0)")
        self.remote.fail_body=3  # deletion group 4 empty, then first page for group 1, then fail.
        with patch.object(f,'capture_batch_size',return_value=1):self.run_repair()
        self.assertEqual(self.state()['status'],'failed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM source WHERE game=1').fetchone()[0],0)
        self.assertTrue(repair.active(self.db,'source'));queries=len(self.remote.queries)
        with patch.object(f,'capture_batch_size',return_value=1):self.run_repair()
        self.assertEqual(self.state()['status'],'complete')
        resumed=[q for q in self.remote.queries[queries:] if 'SELECT src.*' in q and '"game" = 1' in q]
        self.assertIn('("game","uid") > (1,E',resumed[0])
        self.assertEqual(self.state()['rows'],4)

    def test_source_change_during_body_never_replaces_old_group(self):
        self.remote.db.execute('DELETE FROM source WHERE game=1')
        # First body request concerns deleted group 4; mutate changed group 3 then.
        self.remote.mutate_body=lambda:self.remote.db.execute("UPDATE source SET value='newer' WHERE game=3")
        self.run_repair()
        self.assertEqual(self.state()['status'],'failed')
        self.assertEqual(self.db.execute('SELECT value FROM source WHERE game=3').fetchone()[0],'old')
        self.run_repair();self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.db.execute('SELECT value FROM source WHERE game=3').fetchone()[0],'newer')

    def test_final_count_drift_rechecks_summaries_without_redownloading_unchanged_rows(self):
        self.remote.final_count_delta=1;self.run_repair()
        self.assertEqual(self.state()['status'],'failed');requests=self.remote.body_requests
        self.remote.final_count_delta=0;self.run_repair()
        self.assertEqual(self.state()['status'],'complete');self.assertEqual(self.remote.body_requests,requests)

    def test_cost_limit_splits_integer_ranges_including_negative_and_zero(self):
        self.remote.db.executemany('INSERT INTO source VALUES(?,?,?,?)',[(-4,'u','negative',0),(0,'u','zero',None)])
        self.remote.summary_limit=2;self.run_repair()
        self.assertEqual(self.state()['status'],'complete');self.assertEqual(self.state()['rows'],5)
        self.assertGreater(len([q for q in self.remote.queries if 'string_agg' in q]),3)

    def test_large_summary_splits_before_processing_more_than_256_groups(self):
        self.db.execute('DELETE FROM source');self.remote.db.execute('DELETE FROM source')
        rows=[(i,'u','same',1) for i in range(300)]
        self.db.executemany('INSERT INTO source VALUES(?,?,?,?)',rows);self.remote.db.executemany('INSERT INTO source VALUES(?,?,?,?)',rows)
        self.db.execute("UPDATE _capture_state SET rows=300,offset=300");self.db.commit()
        self.run_repair();self.assertEqual(self.state()['status'],'complete');self.assertEqual(self.remote.body_requests,0)
        self.assertGreater(len([q for q in self.remote.queries if 'string_agg' in q]),3)

    def test_unicode_quotes_backslashes_empty_keys_and_nulls_round_trip(self):
        self.remote.db.execute('DELETE FROM source')
        rows=[(1,'','日本語\n"引用"\\path',None),(1,"a'\\b",'更新',True)]
        self.remote.db.executemany('INSERT INTO source VALUES(?,?,?,?)',rows)
        with patch.object(f,'capture_batch_size',return_value=1):self.run_repair()
        self.assertEqual(self.state()['status'],'complete')
        self.assertEqual(self.values(),rows)

    def test_schema_change_rejects_existing_staging(self):
        self.run_repair(max_pages=1);self.table['columns'][2]['max_length']=10
        self.run_repair();self.assertEqual(self.state()['status'],'failed')
        self.assertIn('schema changed',self.state()['error'])

    def test_unsupported_numeric_serialization_fails_closed(self):
        self.table['columns'][2]['type']='numeric'
        self.assertFalse(repair.supported(self.table))
        with self.assertRaises(ValueError):repair.repair(self.db,self.table,self.remote,delay=0)

    def test_public_review_filter_applies_to_summaries_and_changed_bodies(self):
        self.db.execute('ALTER TABLE source RENAME TO userreview_test')
        self.db.execute("UPDATE _capture_state SET name='userreview_test'")
        self.table['table_name']='userreview_test'
        self.remote.db.execute("INSERT INTO myuserview VALUES('u',1)")
        self.db.commit();repair.repair(self.db,self.table,self.remote,delay=0)
        for q in self.remote.queries:
            if 'FROM pg_attribute' not in q:self.assertIn('visibility.review_private IS TRUE',q)
        self.assertEqual(self.db.execute('SELECT count(*) FROM userreview_test').fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT status FROM _capture_state WHERE name='userreview_test'").fetchone()[0],'complete')


if __name__=='__main__':unittest.main()
