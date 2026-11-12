from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import archiver as a


class CrawlResumeTests(unittest.TestCase):
    def test_cached_html_keeps_original_capture_and_fetch_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = a.Store(tmp)
            path = store.root / "html" / "saved.html"
            path.parent.mkdir()
            path.write_bytes(b"<h1>saved</h1>")
            url = a.HOLY
            actual = url.replace("20181025223313", "20181230100432")
            store.db.execute("INSERT INTO pages(url,site,path,fetched_at,status,response_url) VALUES(?,?,?,?,?,?)", (url,"holyseal","html/saved.html","2018-12-30","ok",actual))
            store.db.commit()
            network = Mock()
            cached = a.CachedPageFetcher(store,network)
            with cached.open(url) as response:
                self.assertEqual(response.read(),b"<h1>saved</h1>")
                self.assertEqual(response.geturl(),actual)
                self.assertEqual(response.fetched_at,"2018-12-30")
            network.open.assert_not_called()
            path.unlink()
            cached.open(url)
            network.open.assert_called_once_with(url,None)
            store.db.close()

    def test_http_failure_does_not_replay_stale_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            (store.root/"old.html").write_text("stale")
            store.db.execute("INSERT INTO pages(url,path,status,error) VALUES(?,?,?,?)", (a.HOLY,"old.html","failed","HTTP Error 404"))
            store.db.commit()
            network=Mock()
            a.CachedPageFetcher(store,network).open(a.HOLY)
            network.open.assert_called_once_with(a.HOLY,None)
            store.db.close()
