import hashlib
import json
import tempfile
import unittest
import archiver as a
import bulk
from test_archiver import HTML,PAYLOAD


class PostedShaReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.db.close)
        self.sha=hashlib.sha256(PAYLOAD).hexdigest()
        self.donor=self.add('donor','Publisher A')
        self.public()

    def add(self,ident,brand):
        record=a.parse_detail(HTML,'https://source.test/'+ident,'lathercraft')[0]
        record.update(id=ident,brand=brand);self.store.add(record);return record

    def public(self):
        self.store.update('donor',status='archived',md5=hashlib.md5(PAYLOAD).hexdigest(),sha256=self.sha,size=len(PAYLOAD),remote_key='vn-archive/mirrors/files/'+self.sha,remote_sha256=self.sha,remote_verified_at=a.now())

    def target(self):
        record=self.add('target','Publisher B');record.update(expected_md5=None,expected_sha256=self.sha)
        self.store.add(record);return record

    def reuse(self):
        return bulk.reuse_verified(self.store,self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone(),'vn-archive','mirrors')

    def test_posted_sha_links_exact_bytes_without_merging_manufacturers(self):
        record=self.target();before=self.store.db.execute("SELECT data FROM items WHERE id='target'").fetchone()[0]
        self.assertTrue(self.reuse())
        row=self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
        self.assertTrue(bulk.publicly_archived(row,'vn-archive','mirrors'))
        self.assertEqual(row['data'],before);self.assertEqual(row['reused_from'],'donor')
        makers=self.store.db.execute('SELECT DISTINCT p.manufacturer_id FROM item_catalog c JOIN products p ON p.id=c.product_id').fetchall()
        self.assertEqual(len(makers),2)

    def test_current_md5_size_and_sha_constraints_all_remain_binding(self):
        for change in [{'expected_md5':'0'*32},{'expected_size':len(PAYLOAD)+1},{'expected_sha256':'0'*64}]:
            record=self.target();record.update(change);self.store.add(record)
            self.assertFalse(self.reuse())

    def test_sha_does_not_require_missing_manufacturer_or_md5(self):
        record=self.target();record['brand']=None;self.store.add(record)
        self.assertTrue(self.reuse())
        self.assertIsNone(json.loads(self.store.db.execute("SELECT data FROM items WHERE id='target'").fetchone()[0])['brand'])

    def test_invalid_posted_sha_is_never_ignored(self):
        for value in ['a'*63,'g'*64,17]:
            record=self.target();record.update(expected_sha256=value,expected_md5=self.donor['expected_md5'],brand=self.donor['brand']);self.store.add(record)
            self.assertFalse(self.reuse())

    def test_quarantined_donor_is_excluded(self):
        record={**self.donor,'fields':{**self.donor['fields'],'source_association_quarantined':True}}
        self.store.add(record);self.target();self.assertFalse(self.reuse())

    def test_quarantined_target_is_excluded(self):
        record=self.target();record['fields']={**record['fields'],'source_association_quarantined':True};self.store.add(record)
        self.assertFalse(self.reuse())

    def test_incomplete_or_wrong_public_proof_is_excluded(self):
        self.target()
        for fields in [{'remote_verified_at':None},{'remote_sha256':'0'*64},{'remote_key':'other/bucket/files/'+self.sha}]:
            self.public();self.store.update('donor',**fields);self.assertFalse(self.reuse())

    def test_sha_lookup_uses_the_partial_index(self):
        plan=self.store.db.execute('EXPLAIN QUERY PLAN SELECT * FROM items WHERE sha256=?',(self.sha,)).fetchall()
        self.assertIn('items_sha256',' '.join(str(tuple(row)) for row in plan))


if __name__=='__main__':unittest.main()
