import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import mirror_sources as m


class CrawlMemoryLimitTests(unittest.TestCase):
    def test_bounded_workers_still_visit_every_source(self):
        sources=[m.definitions()[key] for key in ['getchu','dmm','dlsite','moepedia']]
        guard=threading.Lock();two_started=threading.Event();active=peak=0;visited=[]
        def crawl(store,source,fetcher,mode,max_pages):
            nonlocal active,peak
            with guard:
                active+=1;peak=max(peak,active);visited.append(source['key'])
                if active==2:two_started.set()
            try:
                if not two_started.wait(3):raise RuntimeError('two permitted workers did not start')
                time.sleep(.01)
                return max_pages,0
            finally:
                with guard:active-=1
        with tempfile.TemporaryDirectory() as root,patch.object(m,'crawl',side_effect=crawl):
            result=m.parallel_crawl(root,sources,mode='live',max_pages=5)
        self.assertEqual(peak,2)
        self.assertEqual(set(visited),{s['key'] for s in sources})
        self.assertTrue(all(value==(5,0) for value in result.values()))

    def test_single_worker_does_not_hold_multiple_crawls_at_once(self):
        sources=[m.definitions()[key] for key in ['getchu','dmm','dlsite']]
        active=peak=0
        def crawl(*args):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            try:time.sleep(.005);return 1,0
            finally:active-=1
        with tempfile.TemporaryDirectory() as root,patch.object(m,'crawl',side_effect=crawl):
            result=m.parallel_crawl(root,sources,workers=1)
        self.assertEqual(peak,1);self.assertEqual(len(result),3)

    def test_invalid_worker_budget_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError,'workers'):
                m.parallel_crawl(root,[],workers=0)


if __name__=='__main__':unittest.main()
