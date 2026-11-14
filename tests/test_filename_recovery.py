import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import filename_recovery as f
from test_archiver import HTML,PAYLOAD


class FilenameRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.sha=hashlib.sha256(PAYLOAD).hexdigest()
        self.target=a.parse_detail(HTML,'https://source.test/target','d_station')[0]
        self.target.update(id='target',filename=None);self.store.add(self.target)
        self.store.update('target',status='archived',md5=hashlib.md5(PAYLOAD).hexdigest(),sha256=self.sha,size=len(PAYLOAD),remote_sha256=self.sha,remote_key='vn-archive/mirrors/files/'+self.sha,remote_verified_at=a.now())

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def donor(self,ident='donor',name='日本語 体験版.zip',brand='Ｂｒａｎｄ',sha=True):
        data=a.parse_detail(HTML,'https://publisher.test/'+ident,'kokoron')[0]
        data.update(id=ident,filename=name,brand=brand,expected_sha256=self.sha if sha else None)
        self.store.add(data);return data

    def test_published_sha_name_restores_catalog_without_changing_public_bytes(self):
        donor=self.donor();before=dict(self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone())
        with patch.object(self.store,'export',side_effect=AssertionError('background recovery must remain SQLite-only')):
            self.assertEqual(f.recover(self.store),{'recovered':1,'unresolved':0,'deferred':0})
        after=dict(self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone());data=json.loads(after['data'])
        self.assertEqual(data['filename'],'日本語 体験版.zip')
        self.assertEqual(data['fields']['filename_recovered_from']['item_id'],donor['id'])
        self.assertEqual({k:v for k,v in after.items() if k!='data'},{k:v for k,v in before.items() if k!='data'})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM filename_recovery_receipts').fetchone()[0],1)
        self.assertEqual(f.recover(self.store)['recovered'],0)

    def test_md5_alone_or_different_manufacturer_does_not_choose_name(self):
        self.donor(sha=False);self.donor('other',brand='Other')
        self.assertEqual(f.recover(self.store)['unresolved'],1)
        self.assertIsNone(json.loads(self.store.db.execute("SELECT data FROM items WHERE id='target'").fetchone()[0])['filename'])

    def test_conflicting_names_and_unsafe_names_are_not_guessed(self):
        self.donor('a',name='a.zip');self.donor('b',name='b.zip');self.donor('unsafe',name='../evil.zip')
        self.assertEqual(f.recover(self.store)['unresolved'],1)
        self.assertFalse(f.safe_name('bad\nname.zip'))

    def test_catalog_failure_rolls_back_name_and_receipt(self):
        self.donor()
        with patch.object(f.catalog,'index_record',side_effect=RuntimeError('catalog failure')):
            with self.assertRaisesRegex(RuntimeError,'catalog failure'):f.recover(self.store)
        self.assertIsNone(json.loads(self.store.db.execute("SELECT data FROM items WHERE id='target'").fetchone()[0])['filename'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM filename_recovery_receipts').fetchone()[0],0)
        self.assertEqual(f.recover(self.store)['recovered'],1)

    def test_busy_download_is_deferred_without_metadata_changes(self):
        self.donor()
        with patch.object(f,'exclusive_file',side_effect=BlockingIOError('busy')):
            self.assertEqual(f.recover(self.store),{'recovered':0,'unresolved':0,'deferred':1})
        self.assertEqual(f.recover(self.store)['recovered'],1)
