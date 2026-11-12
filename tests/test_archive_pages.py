import json
import tempfile
import unittest
from urllib.error import URLError

import archiver as a
import mirror_sources as m
from test_archiver import Fetch,Response,HTML,MD5


class ArchivePageTests(unittest.TestCase):
    outer='https://web.archive.org/web/20060615025215/http://nekoneko-web.multi-band.net/nekoneko-web18/main18.html'
    inner='https://web.archive.org/web/20060615025215if_/http://nekoneko-web.multi-band.net:80/nekoneko-web18/main18.html'

    def wrapper(self,target):return ('<html><iframe id="playback" src="'+target+'"></iframe></html>').encode()

    def test_reads_publisher_frame_and_preserves_modifier_on_http_fallback(self):
        body='<meta charset="Shift_JIS"><a href="left18.html">配布一覧</a>'.encode('cp932')
        fallback=self.inner.replace('https://','http://',1)
        fetch=Fetch([Response(self.wrapper(self.inner),url=self.outer),URLError(ConnectionRefusedError(111,'refused')),
                     Response(body,url=fallback)])
        raw,actual,charset,fetched_at=a.read_archive_page(fetch,self.outer)
        self.assertEqual(raw,body);self.assertEqual(actual,fallback);self.assertTrue(fetched_at)
        self.assertEqual([url for url,_ in fetch.requests],[self.outer,self.inner,fallback])
        self.assertIn('配布一覧',a.decode_html(raw,charset))

    def test_special_unwrapping_never_follows_other_page_date_or_host(self):
        for target in [self.inner.replace('main18.html','foreign.html'),self.inner.replace('20060615025215','20070615025215'),
                       'https://foreign.test/player.html',self.outer,'']:
            with self.subTest(target=target):
                raw=self.wrapper(target);fetch=Fetch([Response(raw,url=self.outer)])
                self.assertEqual(a.read_archive_page(fetch,self.outer)[0],raw)
                self.assertEqual(len(fetch.requests),1)

    def test_cyclic_wrapper_is_not_reported_as_publisher_html(self):
        raw=self.wrapper(self.inner)
        fetch=Fetch([Response(raw,url=self.outer),Response(raw,url=self.inner)])
        with self.assertRaisesRegex(ValueError,'cyclic'):a.read_archive_page(fetch,self.outer)
        self.assertEqual(len(fetch.requests),2)

    def test_mirror_crawl_reads_frames_inside_publisher_body(self):
        body=b'<frameset><frame src="left18.html"><frame src="top18.php"></frameset>'
        fetch=Fetch([Response(self.wrapper(self.inner),url=self.outer),Response(body,url=self.inner)])
        with tempfile.TemporaryDirectory() as folder:
            store=a.Store(folder);m.initialize(store)
            try:
                store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES('nekoneko',?,'archive')",(self.outer,));store.db.commit()
                m.crawl(store,m.definitions()['nekoneko'],fetch,max_pages=1)
                pending={r[0] for r in store.db.execute("SELECT url FROM mirror_frontier WHERE status='pending'")}
                self.assertTrue({a.resolve(self.inner,'left18.html'),a.resolve(self.inner,'top18.php')}<=pending)
                row=store.db.execute('SELECT path FROM pages WHERE url=?',(self.inner,)).fetchone()
                self.assertEqual((store.root/row['path']).read_bytes(),body)
            finally:store.db.close()

    def test_saved_wrapper_can_resume_without_refetching_outer_page(self):
        body=b'<a href="download/index.html">downloads</a>'
        with tempfile.TemporaryDirectory() as folder:
            store=a.Store(folder)
            try:
                path=store.root/'wrapper.html';path.write_bytes(self.wrapper(self.inner))
                store.db.execute("INSERT INTO pages(url,site,path,fetched_at,status,response_url) VALUES(?,?,?,?,?,?)",
                    (self.outer,'nekoneko','wrapper.html',a.now(),'wayback_wrapper',self.outer));store.db.commit()
                network=Fetch([Response(body,url=self.inner)])
                raw,actual,_,_=a.read_archive_page(a.CachedPageFetcher(store,network),self.outer)
                self.assertEqual(raw,body);self.assertEqual(actual,self.inner)
                self.assertEqual([url for url,_ in network.requests],[self.inner])
            finally:store.db.close()

    def test_native_crawl_parses_actual_publisher_not_wrapper(self):
        outer='https://web.archive.org/web/20190102030405/https://www.lathercraft.net/items/1'
        inner=outer.replace('20190102030405/','20190102030405if_/')
        fetch=Fetch([Response(self.wrapper(inner),url=outer),Response(HTML.encode(),url=inner)])
        with tempfile.TemporaryDirectory() as folder:
            store=a.Store(folder)
            try:
                a.crawl(store,fetch,'lathercraft',outer,max_pages=0,max_items=1,export=False)
                rows=store.rows('lathercraft');self.assertEqual(len(rows),1)
                self.assertEqual(json.loads(rows[0]['data'])['expected_md5'],MD5)
                self.assertEqual(store.db.execute('SELECT response_url FROM pages').fetchone()[0],inner)
            finally:store.db.close()

    def test_multi_inside_navigation_stays_in_distribution_catalogue(self):
        source=m.definitions()['multi_inside'];root='http://www.pachi.ac'
        for path in ['/~multi/','/~multi/index.shtml','/~multi/mirror.shtml','/~multi/demo.info.shtml','/~multi/demo.mirror.shtml','/~multi/download/index/game.html','/~multi/mirror/brand.html','/~multi/left.shtml']:
            self.assertTrue(m.navigable(source,root+path),path)
        for path in ['/~multi/diary.shtml','/~multi/diary/201401.shtml','/~multi/bbs.shtml','/~kanisil/index.shtml']:
            self.assertFalse(m.navigable(source,root+path),path)
