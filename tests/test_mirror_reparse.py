from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
from mirror_reparse import reparse,parser_archiver_source,parser_versions,parser_portals_source
import portals


class SavedHtmlReparseTests(unittest.TestCase):
    def test_parses_once_and_rechecks_changed_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            path=Path(tmp)/'saved.html'
            html='<h1>作品A</h1><table><tr><th>サークル名</th><td>ブランドA</td></tr></table><a href="https://trial.dlsite.com/doujin/RJ1000/RJ123_trial.zip">体験版</a>'
            path.write_text(html)
            store.db.execute('INSERT INTO pages(url,site,path,fetched_at,status) VALUES(?,?,?,?,?)',('https://www.dlsite.com/maniax/work/=/product_id/RJ123.html','dlsite','saved.html',a.now(),'ok'));store.db.commit()
            with patch.object(store,'export',side_effect=AssertionError('background reparse must remain SQLite-only')):
                self.assertEqual(reparse(store),(1,1,0))
            self.assertFalse((Path(tmp)/'manifest.json').exists())
            self.assertEqual(reparse(store),(0,0,0))
            path.write_text(html.replace('RJ123_trial.zip','RJ123_trial2.zip'))
            self.assertEqual(reparse(store),(1,1,0))
            self.assertEqual(len(store.rows()),2)
            store.db.close()


class ParserVersionTests(unittest.TestCase):
    def test_publication_changes_do_not_invalidate_parser_version(self):
        before=b'def parse_detail(html): return html\ndef publish(store): return 1\n'
        after=before.replace(b'return 1',b'return 2')
        self.assertEqual(parser_archiver_source(before),parser_archiver_source(after))

    def test_shared_parser_and_constants_still_invalidate(self):
        cases=[(b'def parse_detail(html): return 1',b'def parse_detail(html): return 2'),
               (b'def normalize(text): return 1',b'def normalize(text): return 2'),
               (b'EXT="zip"',b'EXT="zip|exe"'),
               (b'class DOM:\n def text(self): return 1',b'class DOM:\n def text(self): return 2')]
        for before,after in cases:
            with self.subTest(before=before):self.assertNotEqual(parser_archiver_source(before),parser_archiver_source(after))

    def test_exact_legacy_identity_is_promoted_without_reparsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=a.Store(tmp)
            try:
                path=Path(tmp)/'saved.html';path.write_text('<h1>作品A</h1><table><tr><th>サークル名</th><td>ブランドA</td></tr></table><a href="https://trial.dlsite.com/doujin/RJ1000/RJ123_trial.zip">体験版</a>')
                url='https://www.dlsite.com/maniax/work/=/product_id/RJ123.html'
                store.db.execute('INSERT INTO pages(url,site,path,status) VALUES(?,?,?,?)',(url,'dlsite','saved.html','ok'));store.db.commit()
                self.assertEqual(reparse(store),(1,1,0));current,legacy=parser_versions('dlsite')
                before=store.db.execute('SELECT parsed_at,source_hash FROM mirror_reparse').fetchone()[:]
                store.db.execute('UPDATE mirror_reparse SET parser_version=?',(legacy,));store.db.commit()
                with patch.object(portals,'parse_dlsite',side_effect=AssertionError('exact legacy source must not reparse')):
                    self.assertEqual(reparse(store),(0,0,0))
                row=store.db.execute('SELECT parser_version,parsed_at,source_hash FROM mirror_reparse').fetchone()
                self.assertEqual(row[0],current);self.assertEqual(row[1:],before)
                store.db.execute("UPDATE mirror_reparse SET parser_version='unknown-old-version'");store.db.commit()
                with patch.object(portals,'parse_dlsite',wraps=portals.parse_dlsite) as parser:
                    self.assertEqual(reparse(store),(1,1,0))
                parser.assert_called_once()
            finally:store.db.close()


class IndependentSourceVersionTests(unittest.TestCase):
    def test_suezo_edit_does_not_invalidate_unrelated_parser(self):
        before=b'def parse_suezou(html): return 1\ndef parse_dlsite(html): return 3\n'
        after=before.replace(b'return 1',b'return 2')
        self.assertEqual(parser_portals_source(before,'dlsite'),parser_portals_source(after,'dlsite'))
        self.assertNotEqual(parser_portals_source(before,'suezou'),parser_portals_source(after,'suezou'))

    def test_actual_dependency_keeps_full_fingerprint(self):
        before=b'def parse_suezou(html): return 1\ndef parse_dlsite(html): return parse_suezou(html)\n'
        after=before.replace(b'return 1',b'return 2')
        self.assertNotEqual(parser_portals_source(before,'dlsite'),parser_portals_source(after,'dlsite'))

    def test_shared_helper_changes_still_invalidate_unrelated_parser(self):
        before=b'def helper(html): return 1\ndef parse_suezou(html): return 2\n'
        after=before.replace(b'return 1',b'return 3')
        self.assertNotEqual(parser_portals_source(before,'dlsite'),parser_portals_source(after,'dlsite'))
