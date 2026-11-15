"""Visit explicit sources in finite fresh processes with bounded parallelism."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import time

import archiver as a
from archive_lock import exclusive_file
from delegated_archive import _site


def one_round(sites,*,root='archive',limit=2,scan_limit=100,profile='hf',workers=2,run=None):
    sites=list(dict.fromkeys(_site(site) for site in sites))
    if not sites:raise ValueError('at least one explicit source required')
    if type(workers) is not int or workers not in (1,2):raise ValueError('one or two source workers required')
    if any(type(value) is not int or value<=0 for value in (limit,scan_limit)):
        raise ValueError('positive pass bounds required')
    run=run or subprocess.run
    def visit(site):
        try:
            result=run([sys.executable,'-u',str(Path(__file__).with_name('delegated_archive.py')),
                '--root',str(root),'--site',site,'--limit',str(limit),'--scan-limit',str(scan_limit),'--profile',profile],
                stdin=subprocess.DEVNULL,check=False)
            row=dict(site=site,exit_code=result.returncode)
        except Exception as error:
            row=dict(site=site,exit_code=None,error_type=type(error).__name__)
        print('delegated source pass exit',site,row['exit_code'],row.get('error_type',''),flush=True)
        return row
    # These are finite passes, never permanent --watch children. Each completed
    # source frees a slot for the next, and SQLite retains the next scan ID.
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='source-pass') as pool:
        return list(pool.map(visit,sites))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='archive');p.add_argument('--site',action='append',required=True,type=_site)
    p.add_argument('--limit',type=a.positive,default=2);p.add_argument('--scan-limit',type=a.positive,default=100)
    p.add_argument('--profile',default='hf');p.add_argument('--workers',type=int,choices=(1,2),default=2)
    p.add_argument('--watch',action='store_true');p.add_argument('--interval',type=a.positive,default=120)
    args=p.parse_args(argv)
    with exclusive_file(Path(args.root)/'.claims/delegated-sources.lock',blocking=False):
        while True:
            results=one_round(args.site,root=args.root,limit=args.limit,scan_limit=args.scan_limit,profile=args.profile,workers=args.workers)
            if not args.watch:return int(any(row['exit_code']!=0 for row in results))
            print('delegated source round finished',len(results),flush=True)
            time.sleep(args.interval)


if __name__=='__main__':raise SystemExit(main())
