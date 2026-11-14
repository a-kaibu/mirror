import gc
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import archiver as a
import archive_watch as w
from archive_lock import exclusive_file
from test_archiver import HTML


class WatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=tmp.name
        store=a.Store(self.root)
        record=a.parse_detail(HTML,'https://source.test/item','lathercraft')[0];record['id']='target';store.add(record);store.db.close()
        self.factory=a.Store
    def test_watch_forwards_explicit_runner_choice_and_keeps_source_claim(self):
        for enabled in [False, True]:
            stop=SimpleNamespace(is_set=lambda:False,wait=lambda delay:True)
            def transfer(store,*args,**kwargs):
                self.assertEqual(kwargs['runner_upload'],enabled)
                self.assertEqual(kwargs['site'],'lathercraft')
                with self.assertRaises(BlockingIOError):
                    with exclusive_file(Path(self.root)/'.claims/watch-lathercraft.lock',blocking=False):pass
            with self.subTest(enabled=enabled),patch.object(w,'ready',return_value=1),patch.object(w,'archive_batch',side_effect=transfer) as batch:
                w.watch(self.root,'lathercraft',stop,runner_upload=enabled)
                batch.assert_called_once()
            with exclusive_file(Path(self.root)/'.claims/watch-lathercraft.lock',blocking=False):pass

    def test_busy_snapshot_is_closed_and_next_iteration_uses_fresh_connection(self):
        stores=[];waits=[];calls=[];codes=[]
        writer=sqlite3.connect(self.root+'/index.sqlite3',timeout=.1)
        def factory(root):
            store=self.factory(root);stores.append(store);return store
        def ready(store,site):
            calls.append(store)
            if len(calls)==1:
                store.db.execute('BEGIN');store.db.execute("SELECT error FROM items WHERE id='target'").fetchone()
                writer.execute("UPDATE items SET error='committed by other writer' WHERE id='target'");writer.commit()
                try:store.db.execute("UPDATE items SET error='stale writer' WHERE id='target'")
                except sqlite3.OperationalError as error:codes.append(error.sqlite_errorcode);raise
            self.assertEqual(store.db.execute("SELECT error FROM items WHERE id='target'").fetchone()[0],'committed by other writer')
            return 1
        def transfer(store,*args,**kwargs):
            store.db.execute("UPDATE items SET error='work resumed' WHERE id='target'");store.db.commit()
        def wait(delay):
            waits.append(delay)
            if len(waits)==1:
                with self.assertRaises(sqlite3.ProgrammingError):stores[0].db.execute('SELECT 1')
                self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
                return False
            return True
        stop=SimpleNamespace(is_set=lambda:False,wait=wait)
        try:
            with patch.object(w.a,'Store',side_effect=factory),patch.object(w,'ready',side_effect=ready),patch.object(w,'archive_batch',side_effect=transfer) as batch:
                w.watch(self.root,'mimimaid',stop)
            self.assertEqual(codes,[sqlite3.SQLITE_BUSY_SNAPSHOT]);self.assertEqual(waits,[300,60]);self.assertEqual(len(stores),2)
            self.assertIsNot(stores[0].db,stores[1].db);batch.assert_called_once()
            self.assertEqual(writer.execute("SELECT error FROM items WHERE id='target'").fetchone()[0],'work resumed')
            with self.assertRaises(sqlite3.ProgrammingError):stores[1].db.execute('SELECT 1')
            with exclusive_file(Path(self.root)/'.claims/watch-mimimaid.lock',blocking=False):pass
        finally:writer.close()
    def test_initialization_failure_retries_without_terminating_source_watch(self):
        stores=[];waits=[];attempts=[]
        def factory(root):
            attempts.append(1)
            if len(attempts)==1:raise sqlite3.OperationalError('fixture initialization busy')
            store=self.factory(root);stores.append(store);return store
        def wait(delay):waits.append(delay);return len(waits)==2
        stop=SimpleNamespace(is_set=lambda:False,wait=wait)
        with patch.object(w.a,'Store',side_effect=factory),patch.object(w,'ready',return_value=0) as ready:
            w.watch(self.root,'mimimaid',stop)
        self.assertEqual(waits,[300,60]);self.assertEqual(len(stores),1);ready.assert_called_once()
        with self.assertRaises(sqlite3.ProgrammingError):stores[0].db.execute('SELECT 1')
    def test_cooldown_releases_exception_payloads_and_read_snapshot(self):
        refs=[];stores=[];waits=[];writer=sqlite3.connect(self.root+'/index.sqlite3',timeout=.1)
        class Payload:
            def __init__(self):self.data=bytearray(1024**2)
        def factory(root):
            store=self.factory(root);stores.append(store);return store
        def transfer(store,*args,**kwargs):
            payload=Payload();refs.append(weakref.ref(payload))
            store.db.execute('BEGIN');store.db.execute('SELECT id FROM items').fetchone()
            raise TimeoutError('fixture network failure')
        def wait(delay):
            waits.append(delay);gc.collect();self.assertIsNone(refs[0]())
            self.assertFalse(stores[0].db.in_transaction)
            writer.execute("UPDATE items SET error='another writer continues' WHERE id='target'");writer.commit()
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
            return True
        stop=SimpleNamespace(is_set=lambda:False,wait=wait)
        try:
            with patch.object(w.a,'Store',side_effect=factory),patch.object(w,'ready',return_value=1),patch.object(w,'archive_batch',side_effect=transfer):
                w.watch(self.root,'mimimaid',stop)
            self.assertEqual(waits,[300]);self.assertEqual(len(stores),1)
        finally:writer.close()
