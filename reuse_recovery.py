"""Link waiting source observations to checksum-bound, verified public files."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import time
import archiver as a
from archive_lock import exclusive_file
from bulk import reuse_verified


def recover(store,namespace='vn-archive',bucket='mirrors'):
    rows=store.db.execute("""SELECT DISTINCT t.id FROM items d
      JOIN items t ON json_extract(t.data,'$.expected_md5')=d.md5
      JOIN item_catalog dc ON dc.item_id=d.id JOIN products dp ON dp.id=dc.product_id
      JOIN item_catalog tc ON tc.item_id=t.id JOIN products tp ON tp.id=tc.product_id
      WHERE d.remote_sha256=d.sha256 AND d.remote_verified_at IS NOT NULL
      AND d.remote_key=?||d.sha256 AND d.status IN ('verified','archived')
      AND t.status IN ('failed','pending') AND t.id!=d.id
      AND dp.manufacturer_id=tp.manufacturer_id AND coalesce(json_extract(t.data,'$.brand'),'')!=''
      AND (json_extract(t.data,'$.expected_size') IS NULL OR json_extract(t.data,'$.expected_size')=d.size)
      AND (json_extract(t.data,'$.expected_sha256') IS NULL OR json_extract(t.data,'$.expected_sha256')=d.sha256)
      """,(namespace+'/'+bucket+'/files/',)).fetchall()
    stats={'reused':0,'deferred':0,'unresolved':0}
    for candidate in rows:
        try:
            with exclusive_file(store.root/'.claims'/(hashlib.sha256(candidate['id'].encode()).hexdigest()+'.lock'),blocking=False):
                row=store.db.execute('SELECT * FROM items WHERE id=?',(candidate['id'],)).fetchone()
                if not row or row['status'] not in {'pending','failed'}:continue
                if reuse_verified(store,row,namespace,bucket):stats['reused']+=1
                else:stats['unresolved']+=1
        except BlockingIOError:stats['deferred']+=1
    if stats['reused']:store.export()
    return stats


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='archive');parser.add_argument('--namespace',default='vn-archive');parser.add_argument('--bucket',default='mirrors');parser.add_argument('--watch',action='store_true');args=parser.parse_args()
    if args.watch:
        with exclusive_file(Path(args.root)/'.claims'/'reuse-recovery-watch.lock'):
            while True:
                subprocess.run([sys.executable,'-u',__file__,'--root',args.root,'--namespace',args.namespace,'--bucket',args.bucket],check=True)
                time.sleep(300)
    else:
        with exclusive_file(Path(args.root)/'.claims'/'reuse-recovery.lock'):
            store=a.Store(args.root)
            try:print('verified public reuse',recover(store,args.namespace,args.bucket),flush=True)
            finally:store.db.close()
