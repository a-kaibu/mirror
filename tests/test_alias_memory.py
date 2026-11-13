import json
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

import archiver as a
import catalog as c
from test_archiver import HTML


class AliasMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
    def add(self,ident,brand,large=False):
        record=a.parse_detail(HTML,'https://source.test/items/'+ident,'lathercraft')[0]
        record.update(id=ident,brand=brand)
        if large:record['fields']['fixture']='x'*131072
        self.store.add(record)
    def manufacturer(self,ident):
        return self.store.db.execute('SELECT m.name FROM item_catalog t JOIN products p ON p.id=t.product_id JOIN manufacturers m ON m.id=p.manufacturer_id WHERE t.item_id=?',(ident,)).fetchone()[0]
    def test_only_affected_records_are_reindexed_and_source_state_is_unchanged(self):
        self.add('alias','Typo');self.add('target','Canonical');self.add('unrelated','Separate')
        self.store.update('alias',status='failed',error='fixture source error')
        before=[tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')]
        with patch.object(c,'index_record',wraps=c.index_record) as index:
            c.alias_maker(self.store,'Typo','Canonical')
        self.assertEqual([call.args[1]['id'] for call in index.call_args_list],['alias'])
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')],before)
        self.assertEqual(self.manufacturer('alias'),'Canonical')
        self.assertEqual(self.manufacturer('unrelated'),'Separate')
        with patch.object(c,'index_record',side_effect=AssertionError('idempotent alias should not reindex')):
            c.alias_maker(self.store,'Typo','Canonical')
    def test_all_spellings_of_the_old_manufacturer_move_together(self):
        self.add('a','Spelling A');self.add('b','Spelling B');self.add('final','Canonical')
        c.alias_maker(self.store,'Spelling A','Spelling B')
        with patch.object(c,'index_record',wraps=c.index_record) as index:
            c.alias_maker(self.store,'Spelling B','Canonical')
        self.assertEqual([call.args[1]['id'] for call in index.call_args_list],['a','b'])
        self.assertEqual({self.manufacturer(i) for i in ['a','b','final']},{'Canonical'})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM products').fetchone()[0],1)
    def test_failed_reindex_rolls_back_the_alias_and_catalogue(self):
        self.add('a','Typo');self.add('b','Canonical')
        tables=['manufacturers','manufacturer_aliases','products','item_catalog']
        before={t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in tables}
        with patch.object(c,'index_record',side_effect=RuntimeError('fixture failed reindex')):
            with self.assertRaisesRegex(RuntimeError,'fixture failed reindex'):
                c.alias_maker(self.store,'Typo','Canonical')
        self.assertFalse(self.store.db.in_transaction)
        after={t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in tables}
        self.assertEqual(after,before)
    def test_unrelated_large_source_payloads_are_not_loaded(self):
        self.add('alias','Typo');self.add('target','Canonical')
        for number in range(60):self.add('unrelated-'+str(number),'Separate',large=True)
        tracemalloc.start()
        try:
            c.alias_maker(self.store,'Typo','Canonical')
            self.assertLess(tracemalloc.get_traced_memory()[1],2*1024**2)
        finally:tracemalloc.stop()
        self.assertEqual(self.manufacturer('alias'),'Canonical')

    def test_foreign_key_checks_and_manufacturer_lookup_use_indexes(self):
        for table,column in [('item_catalog','product_id'),('products','manufacturer_id'),('manufacturer_aliases','manufacturer_id')]:
            plan=[r[3] for r in self.store.db.execute('EXPLAIN QUERY PLAN SELECT rowid FROM '+table+' WHERE '+column+'=?',('fixture',))]
            self.assertTrue(any('SEARCH '+table+' ' in step for step in plan),plan)
            self.assertFalse(any('SCAN '+table in step for step in plan),plan)
        for table in ['products','manufacturers']:
            plan=[r[3] for r in self.store.db.execute('EXPLAIN QUERY PLAN DELETE FROM '+table+' WHERE id=?',('fixture',))]
            self.assertFalse(any('SCAN ' in step for step in plan),plan)

    def test_existing_catalogue_index_migration_preserves_data_and_is_idempotent(self):
        self.add('a','Typo');self.add('b','Canonical');c.alias_maker(self.store,'Typo','Canonical')
        tables=['manufacturers','manufacturer_aliases','products','item_catalog','items']
        before={t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in tables}
        for index in ['item_catalog_product','products_manufacturer','manufacturer_aliases_manufacturer']:
            self.store.db.execute('DROP INDEX '+index)
        self.store.db.commit()
        c.initialize(self.store);c.initialize(self.store)
        after={t:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in tables}
        self.assertEqual(after,before)
        for table,index in [('item_catalog','item_catalog_product'),('products','products_manufacturer'),('manufacturer_aliases','manufacturer_aliases_manufacturer')]:
            self.assertIn(index,{r[1] for r in self.store.db.execute('PRAGMA index_list('+table+')')})
