import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import archiver as a
import mirror_sources as m
from size_reviews import review

class SizeReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=a.Store(self.tmp.name);self.addCleanup(self.store.db.close)
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:z.writestr('game.d88',b'original disk image')
        self.payload=data.getvalue()
        self.current=Path(self.tmp.name)/'current.zip';self.current.write_bytes(self.payload)
        self.historical=Path(self.tmp.name)/'historical.zip';self.historical.write_bytes(self.payload)
        self.page='http://retropc.net/alice/12.html';target='http://retropc.net/alice/files/GAME.ZIP'
        self.record={'id':'source','site':'alice_archives','page_url':self.page,'original_page_url':self.page,
            'title':'Game','brand':'Brand','release_name':'本編 PC-98版','filename':'GAME.ZIP','mirrors':[target],
            'expected_size':len(self.payload)+7,'expected_md5':None,'expected_sha256':None,'fields':{'distribution_kind':'full'}}
        self.store.add(self.record);self.store.update('source',status='failed',error='published file size mismatch')
        self.html=Path(self.tmp.name)/'source.html';self.html.write_text('<h1>Game</h1>')
        self.store.db.execute('INSERT INTO pages(url,site,path,status) VALUES(?,?,?,?)',(self.page,'alice_archives','source.html','ok'));self.store.db.commit()
        self.capture={'url':'http://web.archive.org/web/20101128212158id_/'+target,'original_url':target,
            'captured_at':'20101128212158','index_digest':base64.b32encode(hashlib.sha1(self.payload).digest()).decode()}

    def test_verified_review_preserves_publisher_claim_and_survives_recrawl(self):
        ident=review(self.store,self.record,self.current,self.historical,self.capture)
        self.assertNotEqual(ident,'source')
        row=self.store.db.execute('SELECT * FROM items WHERE id=?',(ident,)).fetchone();data=json.loads(row['data'])
        self.assertEqual(data['expected_size'],len(self.payload))
        self.assertEqual(data['expected_sha256'],hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(data['fields']['publisher_expected_size'],len(self.payload)+7)
        self.assertEqual(data['fields']['source_version_of'],'source')
        self.assertEqual(row['status'],'pending');self.assertIsNone(row['remote_verified_at'])
        original=json.loads(self.store.db.execute("SELECT data FROM items WHERE id='source'").fetchone()[0])
        self.assertEqual(original['expected_size'],len(self.payload)+7)
        incoming=dict(self.record)
        self.assertEqual(m.add_record(self.store,incoming),ident)
        self.assertEqual(incoming['id'],ident)
        self.assertEqual(incoming['expected_size'],len(self.payload))
        self.assertEqual(len(self.store.rows()),2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],1)

    def test_changed_source_html_or_file_identity_does_not_reuse_a_review(self):
        review(self.store,self.record,self.current,self.historical,self.capture)
        self.html.write_text('<h1>Another source revision</h1>')
        self.assertEqual(self.store.reviewed_record(dict(self.record)),self.record)
        self.html.write_text('<h1>Game</h1>')
        changed={**self.record,'mirrors':['http://retropc.net/alice/files/OTHER.ZIP']}
        self.assertEqual(self.store.reviewed_record(changed),changed)

    def test_different_historical_bytes_or_wrong_index_digest_are_rejected(self):
        self.historical.write_bytes(b'not the same archive')
        with self.assertRaisesRegex(ValueError,'historical copy differs'):
            review(self.store,self.record,self.current,self.historical,self.capture)
        self.historical.write_bytes(self.payload)
        bad={**self.capture,'index_digest':'A'*32}
        with self.assertRaisesRegex(ValueError,'index digest differs'):
            review(self.store,self.record,self.current,self.historical,bad)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],0)

    def test_publisher_checksum_conflicts_are_never_overridden(self):
        bad={**self.record,'expected_md5':'f'*32}
        self.store.add(bad)
        with self.assertRaisesRegex(ValueError,'publisher MD5 contradicts'):
            review(self.store,bad,self.current,self.historical,self.capture)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],0)

    def test_changed_metadata_cannot_be_replaced_by_a_stale_review(self):
        changed={**self.record,'title':'New title'};self.store.add(changed)
        with self.assertRaisesRegex(ValueError,'metadata changed during review'):
            review(self.store,self.record,self.current,self.historical,self.capture)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],0)
        self.assertEqual(json.loads(self.store.db.execute("SELECT data FROM items WHERE id='source'").fetchone()[0])['title'],'New title')

    def test_sha256_only_versions_and_equal_md5_do_not_overwrite_older_builds(self):
        record={**self.record,'id':'versions','expected_size':100,'expected_md5':'a'*32,'expected_sha256':'a'*64}
        one=m.add_record(self.store,dict(record))
        two=m.add_record(self.store,{**record,'expected_sha256':'b'*64})
        three=m.add_record(self.store,{**record,'expected_sha256':'c'*64})
        self.assertEqual(len({one,two,three}),3)
        self.assertEqual(json.loads(self.store.db.execute('SELECT data FROM items WHERE id=?',(one,)).fetchone()[0])['expected_sha256'],'a'*64)
        self.assertEqual(m.add_record(self.store,{**record,'expected_sha256':'b'*64}),two)

    def collection_fetch(self,matched=True):
        from test_archiver import Fetch,Response
        rows=[["timestamp","original","mimetype","statuscode","digest","length"]]
        if matched:rows.append([self.capture['captured_at'],self.record['mirrors'][0],'application/zip','200',self.capture['index_digest'],str(len(self.payload))])
        return Fetch([Response(self.payload,url=self.record['mirrors'][0],headers={'Content-Length':len(self.payload)}),
                      Response(json.dumps(rows).encode()),Response(self.payload,url=self.capture['url'])])

    def test_collector_imports_verified_bytes_and_does_not_refetch_a_reviewed_source(self):
        from size_reviews import collect
        from test_archiver import Fetch
        fetch=self.collection_fetch()
        result=collect(self.store,fetch,publish=False)
        self.assertEqual(result,{'processed':1,'reviewed':1,'unresolved':0})
        reviewed=self.store.db.execute("SELECT * FROM items WHERE id!='source'").fetchone()
        self.assertEqual(reviewed['status'],'verified')
        self.assertTrue((self.store.root/reviewed['path']).is_file())
        self.assertEqual(collect(self.store,Fetch([]),publish=False)['processed'],0)

    def test_unmatched_historical_digest_retains_observation_without_approving(self):
        from size_reviews import collect
        result=collect(self.store,self.collection_fetch(False),publish=False)
        self.assertEqual(result['unresolved'],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM size_reviews').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM artifact_observations').fetchone()[0],1)
        self.assertEqual(self.store.rows()[0]['status'],'failed')

    def test_publication_failure_stops_and_resumes_without_source_refetch(self):
        from unittest.mock import patch
        from size_reviews import collect
        from test_archiver import Fetch
        with patch.object(a,'publish',side_effect=ValueError('public read-back mismatch')):
            with self.assertRaisesRegex(ValueError,'public read-back mismatch'):
                collect(self.store,self.collection_fetch(),publish=True)
        reviewed=self.store.db.execute("SELECT * FROM items WHERE id!='source'").fetchone()
        self.assertEqual(reviewed['status'],'verified')
        with patch.object(a,'publish',return_value=1) as send,patch('bulk.evict_verified',return_value=0):
            collect(self.store,Fetch([]),publish=True)
            send.assert_called_once()
