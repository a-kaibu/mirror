import json
import sqlite3
import tempfile
import tracemalloc
import unittest
import archiver as a
import catalog
import mirror_sources as m
import official as o
from test_archiver import HTML


class MetadataStreamingMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name)
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.db.close)

    def metadata(self):
        self.store.db.execute('CREATE TABLE external_metadata(provider TEXT,entity TEXT,external_id INTEGER,data TEXT,PRIMARY KEY(provider,entity,external_id))')
        self.store.db.execute('CREATE TABLE metadata_links(entity TEXT,external_id INTEGER)')
        brand={'id':1,'brandname':'Maker','url':'https://maker.test/'}
        self.store.db.execute('INSERT INTO external_metadata VALUES(?,?,?,?)',('erogamescape','brand',1,json.dumps(brand)))
        self.store.db.execute("INSERT INTO metadata_links VALUES('brand',1)")
        for number in range(1,81):
            game={'id':number,'brandname':1,'shoukai':f'https://maker.test/game/{number}/','trial_url':'https://maker.test/game/1/download.html' if number==1 else None,'dlsite_id':f'VJ01{number:06d}','notes':'x'*131072}
            self.store.db.execute('INSERT INTO external_metadata VALUES(?,?,?,?)',('erogamescape','game',number,json.dumps(game)))
        self.store.db.commit()

    def test_store_startup_reindexes_without_materializing_source_payloads(self):
        record=a.parse_detail(HTML,'https://source.test/item','lathercraft')[0]
        for number in range(80):
            data={**record,'id':str(number),'fields':{'notes':'x'*131072}}
            self.store.db.execute('INSERT INTO items(id,site,data) VALUES(?,?,?)',(data['id'],data['site'],json.dumps(data)))
        self.store.db.commit()
        tracemalloc.start();reopened=None
        try:
            reopened=a.Store(self.tmp.name)
            _,peak=tracemalloc.get_traced_memory()
            self.assertLess(peak,3*1024**2)
            self.assertEqual(reopened.db.execute('SELECT count(*) FROM item_catalog').fetchone()[0],80)
            reopened.db.execute("UPDATE item_catalog SET kind='wrong'");reopened.db.commit();reopened.db.close();reopened=None
            refreshed=a.Store(self.tmp.name)
            try:self.assertEqual({row[0] for row in refreshed.db.execute('SELECT kind FROM item_catalog')},{catalog.release_info(record)[0]})
            finally:refreshed.db.close()
        finally:
            tracemalloc.stop()
            if reopened is not None:reopened.db.close()

    def test_platform_seeding_streams_every_game(self):
        self.metadata();m.initialize(self.store)
        tracemalloc.start()
        try:
            m.seed_platform(self.store,m.definitions()['dlsite'],'live')
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertLess(peak,3*1024**2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM mirror_frontier WHERE site='dlsite'").fetchone()[0],80)

    def test_platform_seeding_allows_other_writers_while_parsing_metadata(self):
        from unittest.mock import patch
        self.metadata();m.initialize(self.store)
        writer=sqlite3.connect(self.store.root/'index.sqlite3',timeout=.1)
        original=json.loads;parsed=[]
        def load(payload):
            game=original(payload);parsed.append(game['id'])
            if len(parsed)==2:
                writer.execute("UPDATE external_metadata SET data=json_set(data,'$.changed',1) WHERE entity='game' AND external_id=1")
                writer.commit()
            return game
        try:
            with patch.object(m.json,'loads',side_effect=load):
                m.seed_platform(self.store,m.definitions()['dlsite'],'live')
            self.assertEqual(len(parsed),80)
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM mirror_frontier WHERE site='dlsite'").fetchone()[0],80)
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
        finally:writer.close()

    def test_platform_seed_writes_are_bounded_and_keep_existing_frontier_state(self):
        self.metadata();m.initialize(self.store)
        source=m.definitions()['dlsite'];m.seed_platform(self.store,source,'live')
        prior=self.store.db.execute("SELECT url FROM mirror_frontier WHERE site='dlsite' ORDER BY url LIMIT 1").fetchone()[0]
        self.store.db.execute("UPDATE mirror_frontier SET status='captured' WHERE site='dlsite' AND url=?",(prior,))
        for number in range(81,302):
            self.store.db.execute('INSERT INTO external_metadata VALUES(?,?,?,?)',('erogamescape','game',number,json.dumps({'id':number,'dlsite_id':f'VJ01{number:06d}'})))
        self.store.db.commit();statements=[];self.store.db.set_trace_callback(statements.append)
        try:m.seed_platform(self.store,source,'live')
        finally:self.store.db.set_trace_callback(None)
        self.assertEqual(sum(s.startswith('BEGIN') for s in statements),2)
        self.assertEqual(sum(s=='COMMIT' for s in statements),2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM mirror_frontier WHERE site='dlsite'").fetchone()[0],301)
        self.assertEqual(self.store.db.execute("SELECT status FROM mirror_frontier WHERE site='dlsite' AND url=?",(prior,)).fetchone()[0],'captured')

    def test_official_seeds_are_lazy_keep_order_and_release_read_cursor(self):
        self.metadata();tracemalloc.start()
        try:
            seeds=o.seeds(self.store);first=next(seeds)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertLess(peak,3*1024**2)
        self.assertEqual(first[0]['id'],1)
        writer=sqlite3.connect(self.store.root/'index.sqlite3',timeout=.1)
        try:
            writer.execute("UPDATE external_metadata SET data=json_set(data,'$.changed',1) WHERE entity='game' AND external_id=80");writer.commit()
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0,'a seed iterator paused for network work must not pin the WAL')
        finally:writer.close()
        self.assertEqual([first[0]['id']]+[game['id'] for game,brand,url in seeds],[1]+list(range(80,1,-1)))


if __name__=='__main__':unittest.main()
