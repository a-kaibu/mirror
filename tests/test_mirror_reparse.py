from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import archiver as a
from mirror_reparse import reparse


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
