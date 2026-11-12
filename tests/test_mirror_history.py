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

    def test_history_distinguishes_nondefault_ports(self):
        header=['timestamp','original','mimetype','statuscode','digest']
        rows=[header,['20050101000000','http://mirror.test:80/','text/html','200','default'],
              ['20050102000000','http://mirror.test:8000/','text/html','200','custom']]
        self.assertEqual(history(rows,'http://mirror.test/'),['https://web.archive.org/web/20050101000000/http://mirror.test:80/'])
        self.assertEqual(history(rows,'http://mirror.test:8000/'),['https://web.archive.org/web/20050102000000/http://mirror.test:8000/'])

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


class HistoricalLinkRetentionTests(unittest.TestCase):
    def test_removed_link_keeps_checksum_bound_capture_and_its_evidence(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                first=a.parse_detail('<h1>Game</h1><table><tr><th>ブランド</th><td>Brand</td></tr></table><a href="demo.zip">体験版</a>', 'http://source.test/item', 'kokoron')[0]
                first['expected_md5']='1'*32
                first['page_url']='http://web.archive.org/web/20100102030405/http://source.test/item'
                first['mirrors']=['http://web.archive.org/web/20100102030405id_/http://source.test/demo.zip','http://source.test/demo.zip']
                m.add_record(store,first)
                removed=copy.deepcopy(first);removed['page_url']='http://web.archive.org/web/20120102030405/http://source.test/item'
                removed['mirrors']=[];removed['fields']['removed_link']=True
                m.add_record(store,removed)
                self.assertEqual(removed['mirrors'],first['mirrors'])
                proof=removed['fields']['historical_mirror_links']
                self.assertEqual(proof[0]['page_url'],first['page_url'])
                self.assertEqual(proof[0]['expected_md5'],'1'*32)
                self.assertTrue(removed['fields']['removed_link'])
                m.add_record(store,removed)
                self.assertEqual(len(removed['fields']['historical_mirror_links']),2)
            finally:store.db.close()

    def test_builds_and_unchecksummed_links_are_not_combined(self):
        import copy
        for checksum in [None,'1'*32]:
            with self.subTest(checksum=checksum),tempfile.TemporaryDirectory() as tmp:
                store=a.Store(tmp)
                try:
                    first=a.parse_detail('<h1>Game</h1><a href="demo.zip">体験版</a>','http://source.test/item','kokoron')[0]
                    first['expected_md5']=checksum;m.add_record(store,first)
                    second=copy.deepcopy(first);second['mirrors']=[]
                    if checksum:second['expected_md5']='2'*32
                    m.add_record(store,second)
                    self.assertEqual(second['mirrors'],[])
                    if checksum:self.assertNotEqual(second['id'],first['id'])
                finally:store.db.close()

    def test_existing_source_version_retains_only_its_own_observed_links(self):
        import copy
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                first=a.parse_detail('<h1>Game</h1><a href="demo.zip">体験版</a>','http://source.test/item','kokoron')[0]
                first['expected_md5']='1'*32;m.add_record(store,first)
                second=copy.deepcopy(first);second['expected_md5']='2'*32;second['mirrors']=['http://other.test/new.zip']
                m.add_record(store,second)
                removed=copy.deepcopy(first);removed['expected_md5']='2'*32;removed['mirrors']=[]
                m.add_record(store,removed)
                self.assertEqual(removed['id'],second['id'])
                self.assertEqual(removed['mirrors'],['http://other.test/new.zip'])
                self.assertNotIn(first['mirrors'][0],removed['mirrors'])
            finally:store.db.close()


class HistoricalCrawlOrderTests(unittest.TestCase):
    def test_observed_catalogue_link_precedes_other_homepage_dates(self):
        from test_archiver import Fetch,Response
        source=m.definitions()['magic_mirror']
        home='http://web.archive.org/web/20090119144537/http://magics.ddo.jp/'
        detail='http://web.archive.org/web/20090119144537/http://magics.ddo.jp/info/brand/game.html'
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);m.initialize(store)
            try:
                for url in [home,detail]:store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES(?,?,'archive')",(source['key'],url))
                store.db.commit()
                fetch=Fetch([Response(b'<html>catalogue detail</html>',url=detail)])
                m.crawl(store,source,fetch,mode='archive',max_pages=1)
                self.assertEqual(fetch.requests[0][0],detail)
                self.assertEqual(store.db.execute('select status from mirror_frontier where url=?',(home,)).fetchone()[0],'pending')
            finally:store.db.close()

    def test_known_distribution_layouts_precede_catalogues_without_dropping_them(self):
        from test_archiver import Fetch,Response
        for key,catalogue,detail in [
            ('magic_mirror','http://magics.ddo.jp/dl.html','http://magics.ddo.jp/info/brand/game.html'),
            ('xgame','http://www.xgamedata.com/index2.htm','http://www.xgamedata.com/demo6/g6361/')]:
            with self.subTest(site=key),tempfile.TemporaryDirectory() as tmp:
                store=a.Store(tmp);m.initialize(store)
                try:
                    urls=[a.replay(u,'20120503221242') for u in [catalogue,detail]]
                    for url in urls:store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES(?,?,'archive')",(key,url))
                    store.db.commit();fetch=Fetch([Response(b'<html>distribution</html>',url=urls[1])])
                    m.crawl(store,m.definitions()[key],fetch,mode='archive',max_pages=1)
                    self.assertEqual(fetch.requests[0][0],urls[1])
                    self.assertEqual(store.db.execute('select status from mirror_frontier where url=?',(urls[0],)).fetchone()[0],'pending')
                finally:store.db.close()
