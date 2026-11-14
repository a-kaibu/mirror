from contextlib import closing
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError

import archiver as a
import catalog
import catalogue_transport as codec
import catalogue_restore as restore
import pages_snapshot as pages
import snapshot_upload as snapshots
import multipart_upload as multipart
from archive_lock import exclusive_file
import test_snapshot_upload as fixtures
from test_archiver import HTML,PAYLOAD


class PagesSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=a.Store(self.root/'source');self.addCleanup(self.store.db.close)
        self.record=a.parse_detail(HTML,'https://publisher.test/item','lathercraft')[0];self.record['id']='a';self.store.add(self.record)
        sha=hashlib.sha256(PAYLOAD).hexdigest()
        self.store.update('a',status='archived',path=None,sha256=sha,md5=hashlib.md5(PAYLOAD).hexdigest(),size=len(PAYLOAD),remote_sha256=sha,remote_key='vn-archive/mirrors/files/'+sha,remote_verified_at='2026-11-14T00:00:00+00:00')
        self.store.db.execute('CREATE TABLE operational_payload(value TEXT)');self.store.db.execute('INSERT INTO operational_payload VALUES(?)',('not used by Pages',));self.store.db.commit()
        self.viewroot=self.root/'view';self.viewroot.mkdir();self.view=self.viewroot/'index.sqlite3'

    def public_manifest(self,path):
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=sqlite3.Row
            return catalog.manifest(SimpleNamespace(db=db),public=True)

    def full_bytes(self):
        path=self.root/'full.sqlite3'
        with closing(sqlite3.connect(path)) as db:self.store.db.backup(db)
        return path.read_bytes()

    def test_public_manifest_and_built_json_match_complete_ssot(self):
        with patch.object(a,'now',return_value='2026-11-15T00:00:00+00:00'):
            expected=catalog.manifest(self.store,public=True);pages.export(self.store,self.view,reserve=0)
            self.assertEqual(self.public_manifest(self.view),expected)
            with closing(sqlite3.connect(self.view)) as db:
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables,set(pages.TABLES)|{pages.MARKER})
            view=a.Store(self.viewroot)
            try:catalog.build_site(view,self.root/'site')
            finally:view.db.close()
            self.assertEqual(json.loads((self.root/'site/manifest.json').read_text()),expected)
        self.assertEqual(self.store.db.execute('SELECT value FROM operational_payload').fetchone()[0],'not used by Pages')

    def test_source_changes_during_copy_do_not_mix_generations(self):
        other=a.Store(self.store.root);self.addCleanup(other.db.close);copy=pages._copy_table;changed=[]
        def concurrent(db,name):
            copy(db,name)
            if name=='items':
                other.add(dict(self.record,id='b',title='New work',brand='New maker'));changed.append(True)
        with patch.object(pages,'_copy_table',side_effect=concurrent):pages.export(self.store,self.view,reserve=0)
        self.assertEqual(changed,[True]);self.assertEqual(self.store.db.execute('SELECT count(*) FROM items').fetchone()[0],2)
        with closing(sqlite3.connect(self.view)) as db:
            pages.validate_connection(db);self.assertEqual(db.execute('SELECT id FROM items').fetchall(),[('a',)])
            self.assertEqual(db.execute('SELECT count(*) FROM item_catalog').fetchone()[0],1)

    def test_broken_relations_do_not_replace_a_previous_view(self):
        self.view.write_bytes(b'previous view');self.store.db.execute('PRAGMA foreign_keys=OFF');self.store.db.execute('DELETE FROM manufacturers');self.store.db.commit()
        with self.assertRaises((ValueError,sqlite3.IntegrityError)):pages.export(self.store,self.view,reserve=0)
        self.assertEqual(self.view.read_bytes(),b'previous view')

    def test_derivative_cannot_replace_or_seed_the_authoritative_store(self):
        with self.assertRaisesRegex(ValueError,'replace the SSOT'):pages.export(self.store,self.store.root/'index.sqlite3',reserve=0)
        pages.export(self.store,self.view,reserve=0);view=a.Store(self.viewroot)
        try:
            with self.assertRaisesRegex(ValueError,'authoritative source'):pages.export(view,self.root/'another.sqlite3',reserve=0)
        finally:view.db.close()

    def test_explicit_purpose_is_required_before_restore_replaces_destination(self):
        pages.export(self.store,self.view,reserve=0);raw=self.view.read_bytes();target=self.root/'restored.sqlite3';target.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError,'not a complete catalogue'):codec.restore(io.BytesIO(gzip.compress(raw)),target,reserve=0)
        self.assertEqual(target.read_bytes(),b'old')
        codec.restore(io.BytesIO(gzip.compress(raw)),target,reserve=0,pages=True);self.assertEqual(target.read_bytes(),raw)
        opener=Mock(return_value=io.BytesIO(gzip.compress(raw)))
        proof=restore.restore(target,opener=opener,reserve=0,pages=True)
        self.assertEqual(proof['transport'],'pages-gzip');self.assertTrue(opener.call_args.args[0].full_url.endswith('/pages.sqlite3.gz'))

    def test_pages_404_alone_can_bootstrap_from_complete_checkpoint(self):
        absent=HTTPError('https://source.test/pages.sqlite3.gz',404,'missing',{},io.BytesIO())
        raw=self.full_bytes();opener=Mock(side_effect=[absent,io.BytesIO(gzip.compress(raw))])
        proof=restore.restore(self.root/'restored.sqlite3',opener=opener,reserve=0,pages=True)
        self.assertEqual(proof['transport'],'gzip');self.assertEqual(opener.call_count,2)
        self.assertTrue(opener.call_args.args[0].full_url.endswith('/index.sqlite3.gz'))

    def test_invalid_pages_transport_does_not_fall_back_or_replace_output(self):
        pages.export(self.store,self.view,reserve=0);raw=self.view.read_bytes();target=self.root/'restored.sqlite3'
        for body in [b'HTML error',gzip.compress(raw)[:-3],gzip.compress(self.full_bytes())]:
            target.write_bytes(b'old');opener=Mock(return_value=io.BytesIO(body))
            with self.subTest(length=len(body)),self.assertRaises((OSError,EOFError,ValueError,sqlite3.DatabaseError)):
                restore.restore(target,opener=opener,reserve=0,pages=True)
            self.assertEqual(target.read_bytes(),b'old');opener.assert_called_once()
        for error in [HTTPError('https://source.test',status,'error',{},io.BytesIO()) for status in (403,429,503)]+[TimeoutError()]:
            opener=Mock(side_effect=error)
            with self.assertRaises((HTTPError,TimeoutError)):restore.restore(target,opener=opener,reserve=0,pages=True)
            opener.assert_called_once();self.assertEqual(target.read_bytes(),b'old')

    def test_relation_corruption_is_rejected_on_pages_restore(self):
        pages.export(self.store,self.view,reserve=0)
        with closing(sqlite3.connect(self.view)) as db:db.execute('DELETE FROM manufacturers');db.commit()
        target=self.root/'restored.sqlite3';target.write_bytes(b'old')
        with self.assertRaisesRegex(ValueError,'relations'):codec.restore(io.BytesIO(gzip.compress(self.view.read_bytes())),target,reserve=0,pages=True)
        self.assertEqual(target.read_bytes(),b'old')

    def test_pages_upload_resumes_frozen_copy_independently_of_full_checkpoint(self):
        gateway=fixtures.VersionedGateway();key='pages.sqlite3.gz';gateway.objects['index.sqlite3.gz']=b'full checkpoint stays untouched'
        def readback(ns,bucket,name):
            body=gateway.objects[name];return hashlib.sha256(body).hexdigest(),len(body)
        gateway.fail_part=1
        with patch.object(a,'public_digest',side_effect=readback),patch.object(self.store,'export',side_effect=AssertionError('full host JSON export')),patch.object(catalog,'manifest',side_effect=AssertionError('full host manifest')):
            with self.assertRaises(TimeoutError):snapshots.publish(self.store,gateway,'vn-archive','mirrors',compressed=True,pages=True)
            pointer=snapshots.slot(self.store.root,'vn-archive','mirrors',key)/'current.json';state=json.loads(pointer.read_text())
            self.store.add(dict(self.record,id='b'))
            full_claim=hashlib.sha256(multipart.json_identity('vn-archive','mirrors','index.sqlite3.gz').encode()).hexdigest()
            with exclusive_file(self.store.root/'.claims'/('snapshot-'+full_claim+'.lock')),patch.object(pages,'export',side_effect=AssertionError('resumed copy rebuilt')):
                snapshots.publish(self.store,gateway,'vn-archive','mirrors',compressed=True,pages=True)
        self.assertFalse(pointer.exists());self.assertEqual(gateway.objects['index.sqlite3.gz'],b'full checkpoint stays untouched')
        raw=gzip.decompress(gateway.objects[key]);path=self.root/'published.sqlite3';path.write_bytes(raw)
        codec.validate(path,pages=True)
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT id FROM items').fetchall(),[('a',)])
        receipt=json.loads((pointer.parent/'last-verified.json').read_text());self.assertEqual(receipt['sha256'],state['sha256']);self.assertTrue(receipt['verified_at'])

    def test_copy_value_mismatch_preserves_previous_derivative(self):
        self.view.write_bytes(b'previous view');copy=pages._copy_table
        def corrupt(db,name):
            copy(db,name)
            if name=='items':db.execute("UPDATE main.items SET status='pending'")
        with patch.object(pages,'_copy_table',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'source parity'):pages.export(self.store,self.view,reserve=0)
        self.assertEqual(self.view.read_bytes(),b'previous view')
