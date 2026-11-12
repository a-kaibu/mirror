import json
import tempfile
import unittest
import archiver as a
import mirror_sources as m
from mirror_history import history
from test_archiver import HTML

class MirrorHistoryTests(unittest.TestCase):
    def test_keeps_distinct_html_before_closure_and_rejects_other_pages(self):
        target='http://lapistan.jp/list/PULLTOP/index.htm'
        header=['timestamp','original','mimetype','statuscode','digest']
        rows=[header,['20050101000000',target,'text/html','200','old'],['20060101000000',target,'text/html','200','old'],['20100101000000',target,'text/html','200','new'],['20110101000000','http://evil.test/','text/html','200','evil']]
        urls=history(rows,target)
        self.assertEqual(len(urls),2)
        self.assertIn('20050101000000',urls[0])
        self.assertEqual(history([],target),[])

    def test_changed_filename_build_keeps_previous_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            first=a.parse_detail(HTML,'https://example.test/items/1','lathercraft')[0]
            m.add_record(store,first);base=first['id']
            second=json.loads(json.dumps(first));second['expected_md5']='f'*32
            m.add_record(store,second)
            self.assertNotEqual(second['id'],base)
            self.assertEqual(len(store.rows()),2)
            self.assertEqual(json.loads(store.db.execute('SELECT data FROM items WHERE id=?',(base,)).fetchone()[0])['expected_md5'],first['expected_md5'])
            m.add_record(store,json.loads(json.dumps(second)))
            self.assertEqual(len(store.rows()),2)
            store.db.close()

    def test_http_history_transport_and_dates_survive_cache(self):
        from mirror_history import discover
        from test_archiver import Fetch,Response
        from unittest.mock import Mock
        target='http://lapistan.jp/list/PULLTOP/index.htm'
        rows=[['timestamp','original','mimetype','statuscode','digest'],
              ['20050101000000',target,'text/html','200','old'],
              ['20100101000000',target,'text/html','200','new']]
        source={'key':'lapistan','name':'Lapistan','homes':[target],'seeds':[target],'adapter':'mirror','references':[]}
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            fetch=Fetch([Response(json.dumps(rows).encode(),url='http://web.archive.org/cdx/search/cdx?url=page')])
            self.assertEqual(discover(store,fetch,source),2)
            captures=[r[0] for r in store.db.execute('SELECT url FROM mirror_frontier')]
            self.assertTrue(all(u.startswith('http://web.archive.org/web/') for u in captures))
            never=Mock();self.assertEqual(discover(store,never,source),0);never.open.assert_not_called()
            store.db.close()
