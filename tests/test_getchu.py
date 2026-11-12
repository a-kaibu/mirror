import tempfile
import unittest
from unittest.mock import patch
import archiver as a
import mirror_sources as m
from portals import parse_getchu
from test_archiver import Fetch,Response

class GetchuTests(unittest.TestCase):
    def test_embedded_movie_uses_actual_source_and_capture_date(self):
        html='''<meta property="og:title" content="Game (Brand)"><h1 id="soft-title">Game</h1>
        <video getchutitle="Game デモムービー"><source src="http://trial.getchu.com/D100/123/123.mp4"></video>
        <video getchutitle="Unrelated デモムービー" src="http://trial.getchu.com/other.mp4"></video>
        <video getchutitle="Game デモムービー" src="https://other.test/ad.mp4"></video>'''
        page="https://web.archive.org/web/20190611134157/http://www.getchu.com/soft.phtml?id=123"
        rows=parse_getchu(html,page)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]["title"],rows[0]["brand"],rows[0]["release_name"]),("Game","Brand","ムービー"))
        self.assertEqual(rows[0]["filename"],"123.mp4")
        self.assertEqual(rows[0]["mirrors"],["https://web.archive.org/web/20190611134157id_/http://trial.getchu.com/D100/123/123.mp4","http://trial.getchu.com/D100/123/123.mp4"])
        self.assertEqual(parse_getchu(html,"https://www.getchu.com/all/demo.html"),[])

    def test_known_capture_precedes_unverified_availability_requests(self):
        source=m.definitions()["getchu"]
        capture="https://web.archive.org/web/20190611134157/http://www.getchu.com/soft.phtml?id=123"
        fetch=Fetch([Response(b"<html>Saved page</html>",url=capture)])
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp);m.initialize(store)
            store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES(?,?,'archive')",("getchu","https://www.getchu.com/soft.phtml?id=999"))
            store.db.execute("INSERT INTO mirror_frontier(site,url,mode) VALUES(?,?,'archive')",("getchu",capture));store.db.commit()
            try:
                with patch.object(m,"availability",side_effect=AssertionError("known capture must be fetched first")):
                    m.crawl(store,source,fetch,mode="archive",max_pages=1)
            finally:store.db.close()
        self.assertEqual(fetch.requests[0][0],capture)
        self.assertTrue(m.navigable(source,"http://www.getchu.com/all/demo.html?genre=doujin"))
