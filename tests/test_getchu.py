import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import archiver as a
import mirror_sources as m
from portals import parse_getchu
from test_archiver import Fetch,Response

class GetchuTests(unittest.TestCase):
    def test_modern_product_metadata_and_video_agree(self):
        html=Path("tests/fixtures/getchu-modern-product.html").read_text()
        rows=parse_getchu(html,"https://www.getchu.com/item/1037604/?gc=gc")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["title"],"地球防衛隊スターガーディアンズ 第1話")
        self.assertEqual(rows[0]["brand"],"夢かき屋")
        self.assertEqual(rows[0]["filename"],"1037604.mp4")
        self.assertEqual(rows[0]["mirrors"],["https://trial.getchu.com/D100/1037604/1037604.mp4"])
        self.assertEqual(parse_getchu(html,"https://www.getchu.com/item/1037605/"),[])

    def test_modern_brand_requires_agreement_with_document_title(self):
        html=Path("tests/fixtures/getchu-modern-product.html").read_text()
        html=html.replace("| 夢かき屋","| 別ブランド")
        self.assertEqual(parse_getchu(html,"https://www.getchu.com/item/1037604/"),[])

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

    def test_published_http_replay_transport_is_preserved(self):
        page="http://web.archive.org/web/20190611134157/http://www.getchu.com/soft.phtml?id=123"
        html='<meta property="og:title" content="Game (Brand)"><h1 id="soft-title">Game</h1><video getchutitle="Game デモムービー" src="http://trial.getchu.com/123.mp4"></video>'
        rows=parse_getchu(html,page)
        self.assertEqual(rows[0]["mirrors"][0],"http://web.archive.org/web/20190611134157id_/http://trial.getchu.com/123.mp4")
        self.assertEqual(a.resolve(page,"other.html"),"http://web.archive.org/web/20190611134157/http://www.getchu.com/other.html")

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

    def test_trial_image_button_and_doujin_suffix(self):
        page='http://web.archive.org/web/20140806140345/http://www.getchu.com/soft.phtml?id=783919'
        html='''<meta property="og:title" content="Game (Brand)"><h1 id="soft-title">Game <span>[同人]</span></h1>
        <a href="http://trial.getchu.com/783919/game_trial.zip"><img src="/common/images/demo_t.gif"></a>
        <a href="http://trial.getchu.com/999/unrelated.zip"><img src="/common/images/demo_t.gif"></a>
        <a href="https://other.test/783919/ad.zip">体験版</a>
        <a href="http://trial.getchu.com/783919/unknown.zip">unknown</a>'''
        rows=parse_getchu(html,page)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['title'],rows[0]['brand'],rows[0]['release_name']),('Game','Brand','体験版'))
        self.assertEqual(rows[0]['mirrors'][0],'http://web.archive.org/web/20140806140345id_/http://trial.getchu.com/783919/game_trial.zip')

    def test_compromised_filename_requires_separate_checksum_association(self):
        page='http://www.getchu.com/soft.phtml?id=1197909'
        html='''<meta property="og:title" content="Game (Brand)"><h1 id="soft-title">Game</h1>
        <a href="http://trial.getchu.com/D100/1197909/1197909.zip"><img src="/common/images/demo_t.gif"></a>'''
        self.assertEqual(parse_getchu(html,page),[])
