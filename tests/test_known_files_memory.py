import json
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
import archiver as a
import mirror_sources as m
from test_archiver import HTML


class KnownFileMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://donor.test/item','lathercraft')[0]
        self.store.add(self.record)

    def test_index_streams_rows_and_resolves_complete_record_on_demand(self):
        with patch.object(self.store,'rows',side_effect=AssertionError('do not materialize every record')):
            hashes,urls=m.known_files(self.store,'fuzzy2')
        self.assertEqual(hashes[self.record['expected_md5']],[self.record])
        self.assertEqual(urls[m.clean(self.record['mirrors'][0])],[self.record])
        self.assertEqual(m.known_files(self.store,'lathercraft'),({},{}))

    def test_changed_checksum_or_url_cannot_donate_through_stale_index(self):
        hashes,urls=m.known_files(self.store,'fuzzy2')
        updated={**self.record,'expected_md5':'0'*32,'mirrors':['https://different.test/other.zip']}
        self.store.add(updated)
        self.assertEqual(hashes[self.record['expected_md5']],[])
        self.assertEqual(urls[m.clean(self.record['mirrors'][0])],[])

    def test_quarantine_is_rechecked_after_index_creation(self):
        hashes,urls=m.known_files(self.store,'fuzzy2')
        updated={**self.record,'fields':{**self.record['fields'],'source_association_quarantined':True}}
        self.store.add(updated)
        self.assertEqual(hashes[self.record['expected_md5']],[])
        self.assertEqual(urls[m.clean(self.record['mirrors'][0])],[])

    def test_index_does_not_retain_large_unmatched_metadata(self):
        for number in range(80):
            record={**self.record,'id':str(number),'fields':{'notes':'x'*131072}}
            self.store.db.execute('INSERT INTO items(id,site,data) VALUES(?,?,?)',(record['id'],record['site'],json.dumps(record)))
        self.store.db.commit()
        tracemalloc.start()
        try:
            index=m.known_files(self.store,'fuzzy2')
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertLess(peak,2*1024**2,'unmatched detailed records must remain in SQLite')
        self.assertEqual(len(index[0]),1)
        self.assertEqual(len(index[0][self.record['expected_md5']]),81)


if __name__=='__main__':unittest.main()
