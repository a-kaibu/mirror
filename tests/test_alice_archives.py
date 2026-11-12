import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
import archiver as a
from portals import parse_alice_archives,parse_alicefree
from test_archiver import Fetch,Response,PAYLOAD

class AliceArchivesTests(unittest.TestCase):
    def test_platforms_have_separate_exact_sizes(self):
        html='''<font size="5">Rance -光を求めて-</font><table><tr><td>発売元</td><td>アリスソフト</td></tr></table>
        <a href="./files/RANCE_88.ZIP">PC-8801版 ディスクイメージ (909,278 バイト)</a>
        <a href="./files/RANCE_WIN.ZIP">Windows版 アーカイブ (1,550,784 バイト)</a>
        <a href="https://other.test/alice/files/other.zip">Windows版 アーカイブ</a>'''
        rows=parse_alice_archives(html,"http://retropc.net/alice/04.html")
        self.assertEqual(len(rows),2)
        self.assertEqual([r["expected_size"] for r in rows],[909278,1550784])
        self.assertEqual([r["fields"]["platform"] for r in rows],["PC-8801版","Windows版"])
        self.assertNotEqual(rows[0]["id"],rows[1]["id"])
        self.assertEqual(parse_alice_archives(html,"http://retropc.net/alice/menu.html"),[])

    def test_parts_and_booklet_are_associated_with_the_correct_game(self):
        html='''配布フリー宣言
        <p><font size="+1">鬼畜王ランス</font><font size="+4">
        <a href="https://d1.alicefree.fastlast.org/d/kichikuou.part1.rar">分割→１</a>
        <a href="https://d1.alicefree.fastlast.org/d/kichikuou.part2.rar">２</a></font></p>
        <p><font size="+1">鬼畜王ランス 初回限定版に付属のブックレット</font>
        <a href="https://d1.alicefree.fastlast.org/d/zip/kichikuou_book.zip">ZIP初回本</a></p>'''
        rows=parse_alicefree(html,"https://alicefree.fastlast.org/")
        self.assertEqual(len(rows),3)
        self.assertTrue(all(r["title"]=="鬼畜王ランス" for r in rows))
        self.assertEqual([r["fields"]["part_number"] for r in rows[:2]],[1,2])
        self.assertEqual(rows[0]["fields"]["part_set"],"kichikuou")
        self.assertEqual(rows[2]["fields"]["distribution_kind"],"other")

    def test_mixed_paragraph_does_not_merge_different_games(self):
        html='''配布フリー宣言<p><font size="+1">アリスの館3/学園KING/移植456</font>
        <a href="https://d1.alicefree.fastlast.org/d/alice.zip">アリスの館3(PC-98版)</a>
        <a href="https://d1.alicefree.fastlast.org/d/king.rar">学園KING(Win32移植版)(RAR)(※1)</a>
        <a href="https://other.test/foreign.zip">異なるゲーム</a></p>'''
        rows=parse_alicefree(html,"https://alicefree.fastlast.org/")
        self.assertEqual([r["title"] for r in rows],["アリスの館3(PC-98版)","学園KING(Win32移植版)"])

    def test_referer_is_preserved_on_range_restart(self):
        url="http://retropc.net/alice/files/RANCE_WIN.ZIP"
        page="http://retropc.net/alice/04.html"
        record={"site":"alice_archives","original_page_url":page}
        fetch=Fetch([HTTPError(url,416,"range",{},None),Response(PAYLOAD)])
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/"RANCE_WIN.ZIP"
            part=dest.with_name(dest.name+"."+hashlib.sha256(url.encode()).hexdigest()[:16]+".part")
            part.write_bytes(b"partial")
            part.with_suffix(".json").write_text(json.dumps({"validator":"v1"}))
            a.transfer(fetch,url,dest,record)
            self.assertEqual(dest.read_bytes(),PAYLOAD)
        self.assertEqual(fetch.requests[0][1]["Referer"],page)
        self.assertIn("Range",fetch.requests[0][1])
        self.assertEqual(fetch.requests[1][1],{"Referer":page})

    def test_menu_navigation_uses_published_referring_pages(self):
        import mirror_sources as m
        source=m.definitions()["alice_archives"]
        menu=b'<a href="04.html">Rance</a>'
        entry=b'<a href="menu.html">YES</a>'
        product='''<font size="5">Rance</font><table><tr><td>発売元</td><td>アリスソフト</td></tr></table>
        <a href="files/RANCE_WIN.ZIP">Windows版 アーカイブ (1,550,784 バイト)</a>'''.encode()
        fetch=Fetch([Response(menu,url="http://retropc.net/alice/menu.html"),
                     Response(entry,url="http://retropc.net/alice/"),
                     Response(product,url="http://retropc.net/alice/04.html")])
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                self.assertEqual(m.crawl(store,source,fetch,mode="live",max_pages=10),(3,1))
                self.assertEqual(len(store.rows("alice_archives")),1)
            finally:store.db.close()
        self.assertEqual(fetch.requests[0][1],{"Referer":"http://retropc.net/alice/"})
        self.assertEqual(fetch.requests[2][1],{"Referer":"http://retropc.net/alice/menu.html"})
