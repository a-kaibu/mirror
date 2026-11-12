import tempfile
import unittest
import archiver as a
from archive_watch import ready
from test_archiver import HTML


class ArchiveWatcherTests(unittest.TestCase):
    def test_retries_only_new_binary_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);record=a.parse_detail(HTML,'https://example.test/items/1','lathercraft')[0]
            store.add(record);ident=record['id'];store.update(ident,status='failed')
            self.assertEqual(ready(store,'lathercraft'),0)
            url='https://web.archive.org/web/20080101000000id_/http://example.test/demo.zip'
            store.db.execute('INSERT INTO wayback_candidates VALUES(?,?,?,?,?)',(ident,url,'http://example.test/demo.zip','20080101000000','hash'));store.db.commit()
            self.assertEqual(ready(store,'lathercraft'),1)
            store.update(ident,status='failed')
            store.db.execute('INSERT INTO attempts(item_id,url,attempted_at,error) VALUES(?,?,?,?)',(ident,url,a.now(),'404'));store.db.commit()
            self.assertEqual(ready(store,'lathercraft'),0)
            store.db.close()
