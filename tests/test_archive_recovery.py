from datetime import datetime,timezone,timedelta
from email.message import Message
from email.utils import format_datetime
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
import archiver as a
from test_archiver import Response


class ArchiveRecoveryTests(unittest.TestCase):
    def test_loose_patch_and_audio_do_not_discard_whole_product(self):
        html="""<title>Holyseal ≫ Maker ≫ Game</title><table class="fixtbl">
        <tr><td><strong>修正パッチ Ver1.1</strong>2017/05/27</td><td>patch.xp3 (113kB)</td></tr>
        <tr><td><a href="https://mirror.test/patch.xp3">1</a></td></tr></table>
        <table class="dltbl"><tr><td><strong>自己紹介ボイス</strong>2014/10/24</td><td>voice.mp3 (3.34MB)</td></tr>
        <tr><td><a href="https://mirror.test/voice.mp3">1</a></td></tr></table>"""
        records=a.parse_detail(html,a.HOLY,"holyseal")
        self.assertEqual([r["filename"] for r in records],["patch.xp3","voice.mp3"])
        self.assertEqual([r["size_text"] for r in records],["113kB","3.34MB"])
        self.assertEqual(len(records[0]["mirrors"]),2)
        self.assertEqual(records[0]["release_name"],"修正パッチ Ver1.1")

    def test_parenthesized_and_unknown_names_preserve_other_releases(self):
        html="""<title>Holyseal ≫ Maker ≫ Game</title><table class="dltbl">
        <tr><td><strong>デモ</strong>2017/03/01</td><td>movie(mp4).zip (328MB)</td></tr>
        <tr><td><strong>修正ファイル</strong>2002/03/05</td><td>？ (857kB)</td></tr>
        <tr><td><strong>修正ファイル</strong>2002/03/05</td><td>？ (48MB)</td></tr></table>"""
        records=a.parse_detail(html,a.HOLY,"holyseal")
        self.assertEqual(records[0]["filename"],"movie(mp4).zip")
        self.assertEqual(records[0]["size_text"],"328MB")
        self.assertIsNone(records[1]["filename"])
        self.assertEqual(records[1]["fields"]["filename_text"],"？")
        self.assertEqual(len({r["id"] for r in records}),3)
        from catalog import release_info
        self.assertEqual(release_info(records[1])[0],"patch")

    def test_retry_after_seconds_are_not_capped(self):
        self.assertEqual(a.retry_after_delay("600",60),600)
        self.assertEqual(a.retry_after_delay("bad",60),60)

    def test_retry_after_http_date(self):
        future=datetime.now(timezone.utc)+timedelta(seconds=600)
        value=a.retry_after_delay(format_datetime(future,usegmt=True),60)
        self.assertGreater(value,598)
        self.assertLessEqual(value,601)

    def test_last_retry_cooldown_applies_to_next_url(self):
        headers=Message();headers["Retry-After"]="600"
        fetcher=a.Fetcher(delay=0,retries=1)
        fetcher.opener=Mock()
        fetcher.opener.open.side_effect=[HTTPError("https://web.archive.org/a",429,"limited",headers,None),Response(b"ok")]
        with patch.object(a.time,"monotonic",return_value=100),patch.object(a.time,"sleep") as sleep:
            with self.assertRaises(HTTPError):fetcher.open("https://web.archive.org/a")
            with fetcher.open("https://web.archive.org/b") as response:self.assertEqual(response.read(),b"ok")
        self.assertEqual(fetcher.not_before,700)
        self.assertIn(unittest.mock.call(600),sleep.call_args_list)
