import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from archive_lock import exclusive_file
from urllib.error import HTTPError
import archiver as a
import suezou_watch as w
from test_archiver import Fetch, Response


class SuezoProbeTests(unittest.TestCase):
    root=('<title>すえぞうのギャルゲー補完計画</title>'+''.join('<a href="brand_'+str(i)+'.html">Brand</a>' for i in range(5))).encode()

    def test_empty_and_failed_brands_do_not_hide_later_distribution(self):
        error=HTTPError(w.URL+'brand_0.html',404,'missing',{},None)
        fetch=Fetch([Response(self.root,url=w.URL),error,Response(b'<p>empty</p>',url=w.URL+'brand_1.html'),Response(b'<a href="demo.zip">trial</a>',url=w.URL+'brand_2.html')])
        result=w.probe(fetch)
        self.assertTrue(result['healthy'])
        self.assertEqual(len(result['publisher_checks']),3)
        self.assertEqual(result['next_catalog_offset'],3)
        self.assertEqual(len(fetch.requests),4)

    def test_scheduled_checks_rotate_and_preserve_html_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                first=Fetch([Response(self.root,url=w.URL)]+[Response(b'<p>empty</p>',url=w.URL+'brand_'+str(i)+'.html') for i in range(3)])
                result=w.check(store,first,capture=False)
                self.assertFalse(result['healthy'])
                self.assertEqual(result['next_catalog_offset'],3)
                self.assertEqual(len(first.requests),4)
                for page in result['pages']:
                    self.assertEqual(hashlib.sha256((Path(tmp)/page['path']).read_bytes()).hexdigest(),page['sha256'])
                transient=Fetch([Response(b'<title>Unavailable</title>',url=w.URL)])
                self.assertEqual(w.check(store,transient,capture=False)['next_catalog_offset'],3)
                second=Fetch([Response(self.root,url=w.URL),Response(b'<a href="demo.zip">trial</a>',url=w.URL+'brand_3.html')])
                self.assertTrue(w.check(store,second,capture=False)['healthy'])
                self.assertEqual(second.requests[1][0],w.URL+'brand_3.html')
                self.assertEqual(store.db.execute('SELECT count(*) FROM source_health_history').fetchone()[0],3)
                data=json.loads(store.db.execute('SELECT data FROM source_health').fetchone()[0])
                self.assertTrue(data['healthy'])
                self.assertEqual(w.INTERVAL,10800)
            finally:store.db.close()

    def test_foreign_response_is_not_saved_or_treated_as_recovery(self):
        saved=[]
        fetch=Fetch([Response(self.root,url=w.URL),Response(b'<a href="demo.zip">trial</a>',url='http://foreign.test/'),Response(b'empty',url=w.URL+'brand_1.html'),Response(b'empty',url=w.URL+'brand_2.html')])
        self.assertFalse(w.probe(fetch,save_page=lambda *args:saved.append(args))['healthy'])
        self.assertEqual(len(saved),3)
        self.assertTrue(all(url.startswith(w.URL) for url,_,_ in saved))


class NestedSuezoCatalogueTests(unittest.TestCase):
    def test_nested_navigation_can_reach_distribution_with_existing_request_bound(self):
        root='<title>すえぞう</title><a href="top.html">Enter</a>'.encode()
        fetch=Fetch([Response(root,url=w.URL),Response(b'<a href="dl-18info_m.html">Brands</a>',url=w.URL+'top.html'),Response(b'<a href="brand.html">Brand</a>',url=w.URL+'dl-18info_m.html'),Response(b'<a href="demo.zip">Download</a>',url=w.URL+'brand.html')])
        result=w.probe(fetch)
        self.assertTrue(result['healthy']);self.assertEqual(len(fetch.requests),4)
        self.assertIn(w.URL+'brand.html',result['catalog_urls'])

    def test_discovered_urls_survive_until_next_scheduled_check_and_root_failure(self):
        root='<title>すえぞう</title><a href="top.html">Enter</a><a href="old-top.html">Old</a><a href="dl3.html">More</a>'.encode()
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                first=Fetch([Response(root,url=w.URL),Response(b'<a href="middle.html">More</a>',url=w.URL+'top.html'),Response(b'<a href="deep.html">More</a>',url=w.URL+'middle.html'),Response(b'<a href="brand.html">Brand</a>',url=w.URL+'deep.html')])
                result=w.check(store,first,capture=False)
                self.assertFalse(result['healthy']);self.assertEqual(result['next_catalog_offset'],5)
                self.assertIn(w.URL+'brand.html',result['catalog_urls'])
                failed=w.check(store,Fetch([Response(b'Unavailable',url=w.URL)]),capture=False)
                self.assertEqual(failed['catalog_urls'],result['catalog_urls'])
                second=Fetch([Response(root,url=w.URL),Response(b'<a href="demo.zip">Download</a>',url=w.URL+'brand.html')])
                self.assertTrue(w.check(store,second,capture=False)['healthy'])
                self.assertEqual(second.requests[1][0],w.URL+'brand.html')
                self.assertEqual(w.INTERVAL,10800)
            finally:store.db.close()

    def test_frame_targets_are_observed_but_foreign_or_archived_targets_are_not_followed(self):
        html='<iframe src="brands/menu.html"></iframe><a href="http://evil.test/brand.html">Bad</a><a href="http://web.archive.org/web/20000101000000/http://suezou.dyndns.org/brand.html">Old</a><a href="profile.html">Profile</a>'
        self.assertEqual(w.catalogue_links(a.DOM(html).root,w.URL),[w.URL+'brands/menu.html'])


class SuezoYearIndexPriorityTests(unittest.TestCase):
    def test_existing_brand_target_is_checked_before_other_year_indexes(self):
        urls=[w.URL+'dl-18info_2024.html',w.URL+'dl-18info_2023.html',w.URL+'dl-18info_2022.html',w.URL+'brand.html']
        root=('<title>すえぞう</title>'+''.join('<a href="'+url+'">Year</a>' for url in urls[:3])).encode()
        fetch=Fetch([Response(root,url=w.URL),Response(b'<iframe src="brand.html"></iframe>',url=urls[0]),Response(b'<a href="demo.zip">Trial</a>',url=urls[3])])
        result=w.probe(fetch,catalog_urls=urls)
        self.assertTrue(result['healthy'])
        self.assertEqual([url for url,_ in fetch.requests],[w.URL,urls[0],urls[3]])
        self.assertEqual(len(result['publisher_checks']),2)
        self.assertEqual(w.INTERVAL,10800)
    def test_pending_known_descendant_resumes_after_three_children(self):
        urls=[w.URL+'brand.html',w.URL+'year.html',w.URL+'nav-a.html',w.URL+'nav-b.html']
        root='<title>すえぞう</title>'.encode()
        first=Fetch([Response(root,url=w.URL),Response(b'<a href="nav-b.html">Next</a>',url=urls[2]),Response(b'<a href="year.html">Next</a>',url=urls[3]),Response(b'<a href="brand.html">Publisher</a>',url=urls[1])])
        result=w.probe(first,catalog_offset=2,catalog_urls=urls)
        self.assertFalse(result['healthy']);self.assertEqual(len(first.requests),4)
        self.assertEqual(result['next_catalog_offset'],0)
        second=Fetch([Response(root,url=w.URL),Response(b'<a href="demo.zip">Trial</a>',url=urls[0])])
        resumed=w.probe(second,catalog_offset=result['next_catalog_offset'],catalog_urls=result['catalog_urls'])
        self.assertTrue(resumed['healthy']);self.assertEqual(second.requests[1][0],urls[0])


class SuezoDiscoveryClaimTests(unittest.TestCase):
    def test_busy_discovery_preserves_frontier_and_still_starts_body_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                w.m.initialize(store)
                store.db.execute("INSERT INTO mirror_frontier(site,mode,url,status) VALUES('suezou','live',?,'captured')",(w.URL,));store.db.commit()
                with exclusive_file(Path(tmp)/'.claims'/'discovery-suezou.lock'):
                    with patch.object(w,'probe',return_value={'healthy':True}),patch.object(w.m,'crawl') as crawl,patch.object(w.subprocess,'run') as start:
                        self.assertTrue(w.check(store,None)['healthy'])
                        crawl.assert_not_called();start.assert_called_once()
                self.assertEqual(store.db.execute("SELECT status FROM mirror_frontier WHERE site='suezou' AND url=?",(w.URL,)).fetchone()[0],'captured')
                self.assertEqual(store.db.execute("SELECT healthy FROM source_health WHERE site='suezou'").fetchone()[0],1)
            finally:store.db.close()

    def test_refresh_owns_discovery_claim_during_frontier_crawl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            def crawl(*args,**kwargs):
                with self.assertRaises(BlockingIOError):
                    with exclusive_file(Path(tmp)/'.claims'/'discovery-suezou.lock',blocking=False):pass
            try:
                with patch.object(w,'probe',return_value={'healthy':True}),patch.object(w.m,'crawl',side_effect=crawl) as call:
                    w.check(store,None,start_worker=False)
                    call.assert_called_once()
                with exclusive_file(Path(tmp)/'.claims'/'discovery-suezou.lock',blocking=False):pass
            finally:store.db.close()
