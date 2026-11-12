import io
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
import vndb


class VndbTests(unittest.TestCase):
    def test_copy_escapes_null_and_unicode(self):
        self.assertIsNone(vndb.unescape(b'\\N'))
        self.assertEqual(vndb.unescape(b'\\\\N'),r'\N')
        self.assertEqual(vndb.unescape('日本語'.encode()+b'\\tline\\n\\101\\x42\\\\'), '日本語\tline\nAB\\')
        with self.assertRaises(ValueError):vndb.unescape(b'broken\\')

    @unittest.skipUnless(shutil.which('zstd'),'zstd required')
    def test_whole_dump_schema_rows_and_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dump=root/'dump.tar.zst';plain=root/'dump.tar'
            schema='CREATE TABLE vn (\n  id text NOT NULL,\n  title text NOT NULL,\n  patch boolean,\n  labels smallint[],\n  PRIMARY KEY(id)\n);\nCREATE TABLE empty (\n  id integer NOT NULL,\n  PRIMARY KEY(id)\n);\n'
            with tarfile.open(plain,'w') as archive:
                for name,data in [('README.txt',b'Official fixture'),('LICENSE-ODBL.txt',b'License'),('TIMESTAMP',b'2026-11-12'),('schema_plain.sql',schema.encode()),('db/vn','v1\t日本語\\nタイトル\tt\t{1,2}\nv2\t\\\\N\t\\N\t{}\n'.encode()),('db/vn.header',b'id\ttitle\tpatch\tlabels\n'),('db/empty',b''),('db/empty.header',b'id\n')]:
                    info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
            subprocess.run(['zstd','-q',str(plain),'-o',str(dump)],check=True)
            target=vndb.convert(dump,root/'data.sqlite3','https://dl.vndb.org/dump/fixture')
            with sqlite3.connect(target) as db:
                self.assertEqual(db.execute('SELECT * FROM vn ORDER BY id').fetchall(),[('v1','日本語\nタイトル',1,'{1,2}'),('v2',r'\N',None,'{}')])
                self.assertEqual(db.execute('SELECT count(*) FROM empty').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT rows FROM _source_tables WHERE name="vn"').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT content FROM _source_documents WHERE name="LICENSE-ODBL.txt"').fetchone()[0],'License')
            # A corrupted compressed stream cannot replace the last good snapshot.
            previous=target.read_bytes();dump.write_bytes(dump.read_bytes()[:-20])
            with self.assertRaises((ValueError,tarfile.ReadError,EOFError)):vndb.convert(dump,target)
            self.assertEqual(target.read_bytes(),previous)
