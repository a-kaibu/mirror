import hashlib
import tempfile
import unittest
from unittest.mock import Mock,patch

import archiver as a
from test_archiver import HTML,PAYLOAD,Fetch,Response


class TransferSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=a.Store(tmp.name);self.addCleanup(self.store.db.close)
        for ident in ('target','unrelated'):
            record=a.parse_detail(HTML,'https://example.test/items/'+ident,'lathercraft')[0]
            record['id']=ident;self.store.add(record)

    def test_download_and_publish_one_id_without_loading_source_catalogue(self):
        client=Mock();digest=hashlib.sha256(PAYLOAD).hexdigest()
        with patch.object(self.store,'rows',side_effect=AssertionError('full catalogue loaded')):
            self.assertEqual(a.download(self.store,Fetch([Response(PAYLOAD)]),'lathercraft',ident='target',export=False),0)
            with patch.object(a,'s3_client',return_value=client),patch.object(a,'public_digest',return_value=(digest,len(PAYLOAD))):
                self.assertEqual(a.publish(self.store,'vn-archive','mirrors',site='lathercraft',ident='target',metadata=False),1)
        target=self.store.db.execute("SELECT * FROM items WHERE id='target'").fetchone()
        self.assertEqual(target['remote_sha256'],digest)
        self.assertIsNotNone(target['remote_verified_at'])
        self.assertEqual(self.store.db.execute("SELECT status FROM items WHERE id='unrelated'").fetchone()[0],'pending')
        client.upload_file.assert_called_once()

    def test_download_missing_or_other_site_id_keeps_error_and_changes_nothing(self):
        before=[tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')]
        with patch.object(self.store,'rows',side_effect=AssertionError('full catalogue loaded')):
            for site,ident in [('both','missing'),('official','target'),("lathercraft' OR 1=1--",'target')]:
                with self.assertRaisesRegex(ValueError,'source record ID not found'):
                    a.download(self.store,Fetch([]),site,ident=ident,export=False)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM items ORDER BY id')],before)

    def test_publish_wrong_site_remains_empty_without_upload(self):
        client=Mock()
        with patch.object(self.store,'rows',side_effect=AssertionError('full catalogue loaded')),patch.object(a,'s3_client',return_value=client):
            self.assertEqual(a.publish(self.store,'vn-archive','mirrors',site='official',ident='target',metadata=False),0)
        client.upload_file.assert_not_called()

    def test_unrestricted_selection_preserves_order_and_statuses(self):
        for site in ('both','lathercraft','official'):
            self.assertEqual([tuple(r) for r in a.transfer_rows(self.store,site,None)],[tuple(r) for r in self.store.rows(site)])

    def test_id_selection_uses_index_and_requires_catalogue_association(self):
        statements=[];self.store.db.set_trace_callback(statements.append)
        try:self.assertEqual(len(a.transfer_rows(self.store,'lathercraft','target')),1)
        finally:self.store.db.set_trace_callback(None)
        query=next(s for s in statements if s.startswith('SELECT i.*'))
        plan=[r[3] for r in self.store.db.execute('EXPLAIN QUERY PLAN '+query)]
        self.assertTrue(any('SEARCH i ' in p and 'id=?' in p for p in plan),plan)
        self.store.db.execute("DELETE FROM item_catalog WHERE item_id='target'");self.store.db.commit()
        self.assertEqual(a.transfer_rows(self.store,'both','target'),[])
