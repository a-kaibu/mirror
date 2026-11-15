import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest

import archiver as a
import catalog as c
import pages_snapshot
from test_archiver import HTML


class ProductAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=a.Store(self.root)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self,ident,title,brand='Maker'):
        record=a.parse_detail(HTML,'https://example.test/items/'+ident,'lathercraft')[0]
        record.update(id=ident,title=title,brand=brand,filename=ident+'.zip',
                      expected_size=None,expected_md5=None,release_name='修正パッチ')
        self.store.add(record)
        return record,self.product(ident)

    def product(self,ident):
        return self.store.db.execute('SELECT product_id FROM item_catalog WHERE item_id=?',(ident,)).fetchone()[0]

    def item_rows(self):
        return [tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')]

    def review(self,source,target):
        c.alias_product(self.store,source,target,evidence={'official_product_url':'https://example.test/game/'})

    def test_review_groups_public_products_preserves_all_source_columns_and_reparse(self):
        original,source=self.add('source','ExampleGame')
        _,target=self.add('target','Example Game -作品名-')
        sha='1'*64
        self.store.update('source',status='archived',path=None,size=10,md5='2'*32,
            sha256=sha,remote_sha256=sha,remote_key='vn-archive/mirrors/files/'+sha,
            remote_verified_at=a.now())
        before=self.item_rows()
        self.review(source,target)
        self.assertEqual(self.item_rows(),before)
        self.assertEqual(self.product('source'),target)
        public=c.export_manifest(self.store,public=True)
        self.assertEqual([p['id'] for p in public['products']],[target])
        self.assertEqual(sum(f['download_url'] is not None for f in public['files']),1)
        c.index_record(self.store,original);self.store.db.commit()
        self.assertEqual(self.product('source'),target)
        self.assertEqual(self.item_rows(),before)
        self.store.db.close();self.store=a.Store(self.root)
        self.assertEqual(self.product('source'),target)
        self.assertEqual(self.item_rows(),before)

    def test_old_style_insert_and_update_cannot_split_reviewed_product(self):
        original,source=self.add('source','ExampleGame')
        _,target=self.add('target','Example Game')
        self.review(source,target)
        self.store.db.execute('INSERT OR REPLACE INTO item_catalog VALUES(?,?,?,?,?,?)',
            ('source',source,'patch',None,None,None))
        self.assertEqual(self.product('source'),target)
        self.store.db.execute('UPDATE item_catalog SET product_id=? WHERE item_id=?',(source,'source'))
        self.assertEqual(self.product('source'),target)
        another={**original,'id':'new-source','page_url':'https://example.test/items/new-source'}
        self.store.add(another)
        self.assertEqual(self.product('new-source'),target)

    def test_cross_maker_missing_evidence_and_unknown_maker_are_rejected(self):
        _,source=self.add('source','Example')
        _,other=self.add('other','Example','Other Maker')
        before=self.item_rows()
        with self.assertRaises(ValueError):self.review(source,other)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db:self.store.db.execute('INSERT INTO product_aliases VALUES(?,?,?,?)',(source,other,'{}',a.now()))
        with self.assertRaises(ValueError):c.alias_product(self.store,source,other,evidence={})
        self.assertEqual(self.item_rows(),before)
        _,unknown=self.add('unknown','Unknown A',None)
        _,unknown2=self.add('unknown2','Unknown B',None)
        with self.assertRaises(ValueError):self.review(unknown,unknown2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM product_aliases').fetchone()[0],0)

    def test_flat_aliases_reject_cycles_chains_and_conflicting_reviews(self):
        _,source=self.add('source','A');_,target=self.add('target','B');_,third=self.add('third','C')
        self.review(source,target);self.review(source,target)
        for pair in [(target,third),(third,source),(source,third),(source,source)]:
            with self.subTest(pair=pair),self.assertRaises(ValueError):self.review(*pair)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db:self.store.db.execute('UPDATE product_aliases SET product_id=? WHERE alias_id=?',(third,source))
        self.assertEqual(self.product('source'),target)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM product_aliases').fetchone()[0],1)

    def test_manufacturer_review_migrates_alias_and_retains_referenced_products(self):
        source_record,source=self.add('source','Short','Old Maker')
        _,target=self.add('target','Canonical','Old Maker')
        _,new_target=self.add('new-target','Canonical','New Maker')
        self.review(source,target);before=self.item_rows()
        c.alias_maker(self.store,'Old Maker','New Maker')
        self.assertEqual(self.product('source'),new_target)
        self.assertEqual(self.product('target'),new_target)
        self.assertEqual(self.item_rows(),before)
        c.index_record(self.store,source_record)
        self.assertEqual(self.product('source'),new_target)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        aliases=self.store.db.execute('SELECT alias_id,product_id FROM product_aliases').fetchall()
        self.assertEqual(len(aliases),1)
        self.assertEqual(aliases[0][1],new_target)
        self.assertIsNotNone(self.store.db.execute('SELECT id FROM products WHERE id=?',(aliases[0][0],)).fetchone())

    def test_conflicting_manufacturer_reviews_roll_back_atomically(self):
        _,old=self.add('old','Short','Old Maker');_,old_target=self.add('old-target','One','Old Maker')
        _,new=self.add('new','Short','New Maker');_,new_target=self.add('new-target','Two','New Maker')
        self.review(old,old_target);self.review(new,new_target)
        before={table:[tuple(r) for r in self.store.db.execute('SELECT * FROM '+table+' ORDER BY 1')]
                for table in ['items','item_catalog','products','manufacturers','manufacturer_aliases','product_aliases']}
        with self.assertRaises(ValueError):c.alias_maker(self.store,'Old Maker','New Maker')
        for table,rows in before.items():
            self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM '+table+' ORDER BY 1')],rows,table)

    def test_removing_review_and_reindexing_restores_source_group_without_byte_changes(self):
        original,source=self.add('source','A');_,target=self.add('target','B')
        before=self.item_rows();self.review(source,target)
        with self.store.db:
            self.store.db.execute('DELETE FROM product_aliases WHERE alias_id=?',(source,))
            c.index_record(self.store,original)
        self.assertEqual(self.product('source'),source)
        self.assertEqual(self.item_rows(),before)

    def test_pages_snapshot_keeps_reviewed_associations_without_copying_review_table(self):
        _,source=self.add('source','Short');_,target=self.add('target','Canonical')
        self.review(source,target)
        snapshot=self.root/'pages.sqlite3'
        pages_snapshot.export(self.store,snapshot,reserve=0)
        with sqlite3.connect(snapshot) as db:
            pages_snapshot.validate_connection(db)
            self.assertEqual(db.execute("SELECT product_id FROM item_catalog WHERE item_id='source'").fetchone()[0],target)
        restored_root=self.root/'restored';restored_root.mkdir()
        shutil.copyfile(snapshot,restored_root/'index.sqlite3')
        restored=a.Store(restored_root)
        try:
            self.assertEqual([p['id'] for p in c.export_manifest(restored,public=True)['products']],[target])
            self.assertEqual(restored.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        finally:restored.db.close()
