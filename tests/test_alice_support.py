import unittest
from alice_support import parse

GAME={"id":20764,"gamename":"ランス10"}
BRAND={"id":1,"brandname":"アリスソフト"}
PAGE="https://www.alicesoft.com/support/rance10.html"

class AliceSupportTests(unittest.TestCase):
    def test_versions_and_prerequisites_remain_in_their_own_sections(self):
        html='''<div class="cont01">
        <h4>アップデータVer.1.04</h4><p>Ver.1.02以降に適用。MD5: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</p>
        <p><a href="//download.alicesoft.com/rance10/rance10_updater_ver104.zip">ダウンロード</a></p>
        <h4>アップデータVer.1.02</h4><p>Ver.1.01に適用。</p>
        <p><a href="//download.alicesoft.com/rance10/rance10_updater_ver102.zip">ダウンロード</a></p>
        </div><footer><a href="https://download.alicesoft.com/unrelated.zip">ダウンロード</a></footer>'''
        records=parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(records),2)
        first,second=records
        self.assertEqual(first["release_name"],"パッチ ver1.04")
        self.assertEqual(second["release_name"],"パッチ ver1.02")
        self.assertNotIn("Ver.1.01に適用",first["fields"]["適用条件"])
        self.assertNotIn("Ver.1.02以降",second["fields"]["適用条件"])
        self.assertEqual(first["expected_md5"],"a"*32)
        self.assertIsNone(second["expected_md5"])
        self.assertEqual(first["mirrors"],["https://download.alicesoft.com/rance10/rance10_updater_ver104.zip"])

    def test_multiple_files_do_not_share_a_checksum_or_accept_foreign_hosts(self):
        html='''<div class="cont01"><h4>アップデータVer.1.01</h4><p>MD5: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</p>
        <a href="https://download.alicesoft.com/one.zip">DL</a>
        <a href="https://download.alicesoft.com/two.zip">DL</a>
        <a href="https://example.test/three.zip">DL</a></div>'''
        records=parse(html,PAGE,GAME,BRAND)
        self.assertEqual(len(records),2)
        self.assertTrue(all(r["expected_md5"] is None for r in records))

    def test_legacy_hardmode_keeps_required_patch_text(self):
        html='''<center>ハードモード追加<p><a href="https://download.alicesoft.com/galzoo_hard.lzh">ダウンロード</a>
        Ver1.01適用後に更新。製品版セーブとは互換性がありません。</p></center>'''
        page="https://www.alicesoft.com/galzoo/hard.html"
        records=parse(html,page,GAME,BRAND)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]["release_name"],"パッチ ハードモード追加")
        self.assertIn("Ver1.01適用後",records[0]["fields"]["適用条件"])
        self.assertEqual(parse(html,PAGE,GAME,BRAND),[])
