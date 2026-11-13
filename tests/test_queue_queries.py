import hashlib
import tempfile
import unittest
import archiver as a
from test_archiver import HTML


class QueueQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=a.Store(self.tmp.name);self.addCleanup(lambda:self.store.db.close())
        self.expected={}
        for site in ['official','lathercraft']:
            for kind,status in [('trial','pending'),('patch','failed'),('full','archived'),('movie','verified'),('other','pending')]:
                record=a.parse_detail(HTML,'https://example.test/items/1','lathercraft')[0]
                ident=hashlib.sha256((site+kind).encode()).hexdigest()
                record.update(id=ident,site=site,filename=kind+'.zip')
                self.store.add(record);self.store.update(ident,status=status)
                self.store.db.execute('UPDATE item_catalog SET kind=? WHERE item_id=?',(kind,ident))
                self.expected[(site,kind)]=(ident,status)
        self.store.db.commit()

    def test_source_filter_preserves_every_status_and_catalogue_order(self):
        kinds=['trial','patch','full','movie','other']
        for site in ['official','lathercraft']:
            rows=self.store.rows(site)
            self.assertEqual([(r['id'],r['status']) for r in rows],[self.expected[(site,k)] for k in kinds])
            self.assertEqual({r['site'] for r in rows},{site})
        all_rows=self.store.rows()
        expected=[pair for kind in kinds for pair in sorted(self.expected[(site,kind)] for site in ['official','lathercraft'])]
        self.assertEqual([(r['id'],r['status']) for r in all_rows],expected)
        self.assertEqual(self.store.rows("official' OR 1=1--"),[])

    def test_actual_source_query_uses_an_index_instead_of_scanning_all_items(self):
        statements=[];self.store.db.set_trace_callback(statements.append)
        try:self.store.rows('official')
        finally:self.store.db.set_trace_callback(None)
        select=next(q for q in statements if q.startswith('SELECT i.*'))
        plan=[r[3] for r in self.store.db.execute('EXPLAIN QUERY PLAN '+select)]
        self.assertTrue(any('SEARCH i ' in step and 'site=?' in step for step in plan),plan)
        self.assertFalse(any('SCAN i' in step for step in plan),plan)

    def test_status_and_source_queues_remain_indexed_after_status_updates(self):
        queries=[('SELECT id FROM items WHERE status=?',('verified',)),
                 ("SELECT id FROM items WHERE site=? AND status IN ('pending','verified')",('official',))]
        for query,args in queries:
            plan=[r[3] for r in self.store.db.execute('EXPLAIN QUERY PLAN '+query,args)]
            self.assertTrue(any('SEARCH items ' in step for step in plan),plan)
        self.assertEqual(len(self.store.db.execute(queries[0][0],queries[0][1]).fetchall()),2)
        ident=self.expected[('official','trial')][0];self.store.update(ident,status='verified')
        self.assertEqual(len(self.store.db.execute(queries[0][0],queries[0][1]).fetchall()),3)
        self.assertEqual(len(self.store.db.execute(queries[1][0],queries[1][1]).fetchall()),3)

    def test_existing_database_migration_is_idempotent_and_preserves_records(self):
        before=[tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')]
        self.store.db.execute('DROP INDEX items_site_status');self.store.db.execute('DROP INDEX items_status');self.store.db.commit()
        self.store.db.close();self.store=a.Store(self.tmp.name)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')],before)
        indexes={r[1] for r in self.store.db.execute('PRAGMA index_list(items)')}
        self.assertTrue({'items_site_status','items_status'}<=indexes)
        self.store.db.close();self.store=a.Store(self.tmp.name)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')],before)
