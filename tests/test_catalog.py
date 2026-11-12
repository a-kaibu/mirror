import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import archiver as a
import catalog as c
from test_archiver import Fetch, Response, PAYLOAD, HTML

FIXTURES = Path(__file__).parent / "fixtures"


class LiveHTMLTests(unittest.TestCase):
    def test_lathercraft_follows_next_and_stops_at_last_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = a.Store(tmp)
            first = b'<a href="/items/1">one</a><a rel="next" href="/items?page=2">next</a><a href="/items?page=109">last</a>'
            last = b'<a href="/items/2">two</a><a href="/items?page=1">prev</a>'
            fetch = Fetch([Response(first), Response(HTML.encode()), Response(last, url="https://www.lathercraft.net/items?page=2"), Response(HTML.encode(), url="https://www.lathercraft.net/items/2")])
            self.assertEqual(a.crawl(store, fetch, "lathercraft", "https://www.lathercraft.net/items?page=1"), 0)
            self.assertEqual(len(fetch.requests), 4)
            self.assertEqual(len(store.rows()), 2)
            self.assertFalse(any("109" in url or "page=3" in url for url, _ in fetch.requests))
            store.db.close()

    def test_lathercraft_product_not_site_heading(self):
        records = a.parse_detail(a.decode_html((FIXTURES / "lather-2418.html").read_bytes()), "http://www.lathercraft.net/items/2418", "lathercraft")
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["title"], "VenusBlood RAGNAROK International")
        self.assertEqual(r["brand"], "DualTail")
        self.assertEqual(r["expected_size"], 662640604)
        self.assertEqual(r["expected_md5"], "724992e7ccfd119b184aa96e22d00f20")
        self.assertEqual(c.release_info(r)[:2], ("patch", "1.04b"))

    def test_trial_and_movie(self):
        for name, kind in [("lather-2434", "trial"), ("lather-2437", "movie")]:
            r = a.parse_detail((FIXTURES / (name + ".html")).read_text(), "http://www.lathercraft.net/items/1", "lathercraft")[0]
            self.assertEqual(c.release_info(r)[0], kind)
            self.assertTrue(r["brand"])
            self.assertTrue(r["expected_size"])

    def test_holyseal_patch_table_not_just_demo_table(self):
        records = a.parse_detail(a.decode_html((FIXTURES / "holy-patches.html").read_bytes()), "https://web.archive.org/web/20180513121656/http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=13833", "holyseal")
        patches = [r for r in records if c.release_info(r)[0] == "patch"]
        self.assertEqual(len(patches), 3)
        record = next(r for r in patches if r["filename"] == "amayui_ap2.zip")
        self.assertEqual(record["expected_md5"], "f4effd8953817db89c48f73af1b0ee40")
        self.assertEqual(c.release_info(record)[:2], ("patch", "1.04"))
        self.assertEqual(len(record["mirrors"]), 8)

    def test_holyseal_multiline_release_and_unavailable_file(self):
        records = a.parse_detail(a.decode_html((FIXTURES / "holy-detail.html").read_bytes()), "https://web.archive.org/web/20181230100432/http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=14450", "holyseal")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["brand"], "ウグイスカグラ")
        self.assertEqual(records[0]["title"], "空に刻んだパラレログラム")
        self.assertEqual(records[0]["expected_md5"], "5c54c2a8ef90dd755689d4ced0a6d9b3")
        self.assertEqual(len(records[0]["mirrors"]), 6)
        self.assertEqual(records[1]["mirrors"], [])
        self.assertEqual(records[1]["expected_md5"], "00710d208ac643b24b5504a655e016d8")
        self.assertEqual(c.release_info(records[0])[0], "trial")

    def test_wayback_redirect_uses_actual_capture_and_no_year_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = a.Store(tmp)
            url = "https://web.archive.org/web/20181230100432/http://holyseal.net/cgi-bin/mlistview.cgi?prdcode=14450"
            fetch = Fetch([Response((FIXTURES / "holy-detail.html").read_bytes(), url=url)])
            self.assertEqual(a.crawl(store, fetch, "holyseal", url, max_items=1), 0)
            record = json.loads(store.rows()[0]["data"])
            self.assertIn("/20181230100432", record["page_url"])
            store.db.close()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = a.Store(Path(self.tmp.name) / "archive")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def record(self, ident, brand="Brand", site="lathercraft"):
        r = a.parse_detail(HTML, "https://example.test/items/" + ident, "lathercraft")[0]
        return {**r, "id": ident, "brand": brand, "site": site, "filename": "日本語 体験版.zip", "release_name": "体験版"}

    def test_recover_mirrors_requires_published_digest_and_same_maker(self):
        donor = self.record("donor")
        donor["mirrors"] = ["https://storage1.lathercraft.net/recovered.zip"]
        self.store.add(donor)
        holy = self.record("holy", "Ｂｒａｎｄ", "holyseal")
        holy["mirrors"] = []
        self.store.add(holy)
        row = self.store.db.execute("SELECT * FROM items WHERE id='holy'").fetchone()
        self.assertEqual(a.matching_mirrors(self.store, row, holy), donor["mirrors"])
        a.download(self.store, Fetch([Response(PAYLOAD)]), ident="holy")
        updated = self.store.db.execute("SELECT * FROM items WHERE id='holy'").fetchone()
        self.assertEqual(updated["status"], "verified")
        self.assertEqual(updated["source_url"], donor["mirrors"][0])
        self.assertEqual(json.loads(updated["data"])["mirrors"], [])
        for changes in [{"expected_md5": None}, {"expected_md5": "f" * 32}, {"expected_size": 99999}]:
            self.assertEqual(a.matching_mirrors(self.store, row, {**holy, **changes}), [])
        other = self.record("other", "Other", "holyseal")
        self.store.add(other)
        other_row = self.store.db.execute("SELECT * FROM items WHERE id='other'").fetchone()
        self.assertEqual(a.matching_mirrors(self.store, other_row, other), [])

    def test_closed_mirror_sources_can_recover_checksum_bound_live_copy(self):
        donor=self.record("donor")
        donor["mirrors"]=["https://storage1.lathercraft.net/recovered.zip"]
        self.store.add(donor)
        for site in ["kokoron","lapistan","mimimaid","suezou","xgame","magic_mirror","redistribution"]:
            with self.subTest(site=site):
                record=self.record(site,"Ｂｒａｎｄ",site)
                record["mirrors"]=["https://closed.test/old.zip"]
                self.store.add(record)
                fetch=Fetch([Response(PAYLOAD)])
                self.assertEqual(a.download(self.store,fetch,ident=site,export=False),0)
                self.assertEqual(fetch.requests[0][0],donor["mirrors"][0])
                updated=self.store.db.execute("SELECT * FROM items WHERE id=?",(site,)).fetchone()
                self.assertEqual(updated["status"],"verified")
                self.assertEqual(json.loads(updated["data"])["mirrors"],record["mirrors"])
                self.assertEqual(a.matching_mirrors(self.store,updated,{**record,"expected_md5":None}),[])
                self.assertEqual(a.matching_mirrors(self.store,updated,{**record,"expected_size":1}),[])
        other=self.record("other","Other","kokoron");self.store.add(other)
        row=self.store.db.execute("SELECT * FROM items WHERE id='other'").fetchone()
        self.assertEqual(a.matching_mirrors(self.store,row,other),[])

    def test_repaired_movies_are_not_game_patches(self):
        record = self.record("movie")
        record["filename"] = "opening_movie.zip"
        for label in ["オープニングムービー 修正版", "ティザームービー 修正版", "Opening Movie update"]:
            record["release_name"] = label
            self.assertEqual(c.release_info(record)[0], "movie")
        record["release_name"] = "体験版 修正版"
        self.assertEqual(c.release_info(record)[0], "trial")
        record["release_name"] = "デモニオン 修正パッチ"
        self.assertEqual(c.release_info(record)[0], "patch")
        record["release_name"] = "オープニングムービー 修正版"
        self.store.add(record)
        self.store.db.execute("UPDATE item_catalog SET kind='patch' WHERE item_id='movie'")
        self.store.db.commit()
        c.initialize(self.store)
        self.assertEqual(self.store.db.execute("SELECT kind FROM item_catalog WHERE item_id='movie'").fetchone()[0], "movie")

    def test_cross_site_merge_only_after_matching_bytes(self):
        self.store.add(self.record("a"))
        self.store.add(self.record("b", "Ｂｒａｎｄ", "holyseal"))
        before = self.store.export()
        self.assertEqual(len(before["manufacturers"]), 1)
        self.assertEqual(len(before["products"]), 1)
        self.assertEqual(len(before["files"]), 2)
        a.download(self.store, Fetch([Response(PAYLOAD), Response(PAYLOAD)]))
        after = self.store.export()
        self.assertEqual(len(after["files"]), 1)
        self.assertEqual(len(after["files"][0]["sources"]), 2)
        self.assertIsNone(after["files"][0]["download_url"])

    def test_alias_survives_reopen_and_distinct_payloads_stay_distinct(self):
        self.store.add(self.record("a", "メーカー別名"))
        self.store.add(self.record("b", "メーカー正式名"))
        c.alias_maker(self.store, "メーカー別名", "メーカー正式名")
        self.assertEqual(len(self.store.export()["manufacturers"]), 1)
        root = self.store.root
        self.store.db.close()
        self.store = a.Store(root)
        self.store.add(self.record("c", "メーカー別名"))
        self.assertEqual(len(self.store.export()["products"]), 1)
        self.store.update("a", sha256="a" * 64)
        self.store.update("b", sha256="b" * 64)
        self.assertEqual(len(self.store.export()["files"]), 3)

    def test_mirror_updates_keep_bytes_matching_current_published_digest(self):
        record = self.record("a")
        self.store.add(record)
        a.download(self.store, Fetch([Response(PAYLOAD)]))
        sha = hashlib.sha256(PAYLOAD).hexdigest()
        self.store.update("a", remote_sha256=sha, remote_key="vn-archive/mirrors/files/" + sha, remote_verified_at=a.now())
        record["mirrors"].append("https://new-mirror.test/file.zip")
        self.store.add(record)
        self.assertEqual(self.store.rows()[0]["status"], "verified")
        self.assertEqual(self.store.rows()[0]["remote_sha256"], sha)
        record["expected_md5"] = "f" * 32
        self.store.add(record)
        self.assertEqual(self.store.rows()[0]["status"], "pending")
        self.assertIsNone(self.store.rows()[0]["remote_sha256"])

    def test_checksumless_descriptive_update_retains_public_copy(self):
        record=self.record("checksumless");record["expected_md5"]=None
        self.store.add(record);a.download(self.store,Fetch([Response(PAYLOAD)]))
        sha=hashlib.sha256(PAYLOAD).hexdigest()
        self.store.update(record["id"],status="archived",remote_sha256=sha,remote_key="vn-archive/mirrors/files/"+sha,remote_verified_at=a.now())
        record["fields"]["description"]="corrected metadata"
        self.store.add(record)
        row=self.store.db.execute("SELECT * FROM items WHERE id=?",(record["id"],)).fetchone()
        self.assertEqual(row["status"],"archived")
        self.assertEqual(row["remote_sha256"],sha)
        record["mirrors"]=["https://other.test/replaced.zip"]
        self.store.add(record)
        row=self.store.db.execute("SELECT * FROM items WHERE id=?",(record["id"],)).fetchone()
        self.assertEqual(row["status"],"pending")
        self.assertIsNone(row["remote_sha256"])

    def test_public_link_requires_public_verification(self):
        self.store.add(self.record("a"))
        sha = "a" * 64
        self.store.update("a", status="verified", sha256=sha, remote_sha256=sha, remote_key="vn-archive/mirrors/files/" + sha)
        self.assertIsNone(self.store.export()["files"][0]["download_url"])
        self.store.update("a", remote_verified_at=a.now())
        self.assertEqual(self.store.export()["files"][0]["download_url"], a.public_url("vn-archive", "mirrors", "files/" + sha, True))
        self.store.update("a", status="failed")
        self.assertIsNone(self.store.export()["files"][0]["download_url"])

    def test_static_html_escape_subpaths_and_single_json(self):
        r = self.record("a", '<script>alert("x")</script>')
        r["title"] = "作品 <img src=x onerror=alert(1)>"
        self.store.add(r)
        out = Path(self.tmp.name) / "site"
        c.build_site(self.store, out)
        self.assertEqual([p.name for p in out.rglob("*.json")], ["manifest.json"])
        page = next((out / "makers").glob("*.html")).read_text()
        self.assertIn("../index.html", page)
        self.assertIn("&lt;img", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("未収録", page)
        self.assertNotIn('class="download"', page)
        self.assertNotIn("Hugging Face", page)
        self.assertNotIn("HFから", page)
        self.assertNotIn("元のファイル名で保存", page)
        self.assertTrue((out / "sw.js").exists())

    def test_migrates_old_sqlite_without_losing_progress(self):
        legacy = Path(self.tmp.name) / "legacy"
        legacy.mkdir()
        r = self.record("old")
        with closing(sqlite3.connect(legacy / "index.sqlite3")) as db:
            db.executescript("""CREATE TABLE items(id TEXT PRIMARY KEY, site TEXT NOT NULL, data TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', path TEXT, md5 TEXT, sha256 TEXT, size INTEGER,
              source_url TEXT, fetched_at TEXT, error TEXT, remote_key TEXT, remote_sha256 TEXT);
              CREATE TABLE pages(url TEXT PRIMARY KEY, site TEXT, path TEXT, fetched_at TEXT, status TEXT, error TEXT);""")
            db.execute("INSERT INTO items(id,site,data,status) VALUES(?,?,?,?)", ("old", r["site"], json.dumps(r), "failed"))
            db.commit()
        migrated = a.Store(legacy)
        self.assertEqual(migrated.rows()[0]["status"], "failed")
        self.assertEqual(len(migrated.export()["manufacturers"]), 1)
        self.assertIn("response_url", {row[1] for row in migrated.db.execute("PRAGMA table_info(pages)")})
        migrated.db.close()

    def test_publish_deduplicates_bytes_and_snapshot_is_consistent(self):
        self.store.add(self.record("a"))
        self.store.add(self.record("b", site="holyseal"))
        a.download(self.store, Fetch([Response(PAYLOAD), Response(PAYLOAD)]))
        objects = {}
        client = Mock()
        client.upload_file.side_effect = lambda path, bucket, key, **kw: objects.update({key: Path(path).read_bytes()})
        with patch.object(a, "s3_client", return_value=client), patch.object(a, "public_digest", side_effect=lambda ns, bucket, key: (hashlib.sha256(objects[key]).hexdigest(), len(objects[key]))):
            a.publish(self.store, "vn-archive", "mirrors")
        uploads = [call.args[2] for call in client.upload_file.call_args_list if call.args[2].startswith("files/")]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0], "files/" + hashlib.sha256(PAYLOAD).hexdigest())
        snapshot = Path(self.tmp.name) / "snapshot.sqlite3"
        snapshot.write_bytes(objects["index.sqlite3"])
        with closing(sqlite3.connect(snapshot)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE remote_verified_at IS NOT NULL").fetchone()[0], 2)
        self.assertNotIn("manifest.json", objects)


if __name__ == "__main__":
    unittest.main()
