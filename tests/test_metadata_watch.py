import tempfile
import signal
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import archiver as a
import mirror_sources as m
import metadata_watch as w


class MetadataWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=a.Store(self.tmp.name);m.initialize(self.store)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def add(self,site,mode,status='pending'):
        self.store.db.execute('INSERT INTO mirror_frontier(site,url,mode,status) VALUES(?,?,?,?)',(site,'https://source.test/'+site,mode,status));self.store.db.commit()
    def test_pending_sources_exclude_completed_and_native_and_defer_platform_history(self):
        self.add('d_station','archive');self.add('holyseal','archive');self.add('dmm','archive');self.add('dmm','live');self.add('dlsite','live','captured')
        self.assertEqual([s['key'] for s in w.pending_sources(self.tmp.name,'archive')],['d_station'])
        self.assertEqual([s['key'] for s in w.pending_sources(self.tmp.name,'live')],['dmm'])
        self.store.db.execute("UPDATE mirror_frontier SET status='captured' WHERE site='dmm' AND mode='live'");self.store.db.commit()
        self.assertEqual({s['key'] for s in w.pending_sources(self.tmp.name,'archive')},{'d_station','dmm'})
    def test_active_discovery_claim_is_deferred(self):
        self.add('d_station','archive')
        with patch.object(w,'exclusive_file',side_effect=BlockingIOError('busy')):
            self.assertEqual(w.pending_sources(self.tmp.name,'archive'),[])
    def test_batch_uses_existing_shared_crawler_and_page_bound(self):
        self.add('d_station','archive');self.add('getchu','archive')
        with patch.object(m,'parallel_crawl') as crawl:
            self.assertEqual(w.run_batch(self.tmp.name,'archive',100),2)
        args=crawl.call_args.args
        self.assertEqual({s['key'] for s in args[1]},{'d_station','getchu'})
        self.assertEqual(args[2:],('archive',100))
        self.assertEqual(crawl.call_args.kwargs['workers'],1)


class MetadataSchedulingTests(unittest.TestCase):
    def exercise(self,root,ready=None,results=None,stop_after=4,parallel=False,stop_during_launch=False,idle=False,threaded=False):
        stop=threading.Event();handlers={};launched=[];checked=[];waits=[];terminated=[];completed=[]
        counts={'active':0,'peak':0};guard=threading.Lock()
        barrier=threading.Barrier(2) if parallel else None
        codes=iter(results or [0]*20)
        def finish(child,code):
            with guard:
                if child.code is None:counts['active']-=1;child.code=code
        class Child:
            def __init__(self,mode,code):self.mode=mode;self.code=None;self.result=code
            def poll(self):return self.code
            def terminate(self):terminated.append(self.mode);finish(self,-15)
            def wait(self):
                if barrier:barrier.wait(timeout=3)
                finish(self,self.result)
                with guard:completed.append(self.mode);n=len(completed)
                if stop_after is not None and n>=stop_after:handlers[signal.SIGINT]()
                return self.code
        def spawn(command):
            mode=command[command.index('--mode')+1]
            with guard:
                launched.append(mode);counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
            child=Child(mode,next(codes))
            if stop_during_launch:handlers[signal.SIGINT]()
            return child
        def pending(root,mode):
            checked.append(mode)
            return ['source'] if ready is None or ready.get(mode,False) else []
        def wait(interval):
            waits.append(interval)
            if idle:stop.set()
            return stop.is_set()
        errors=[]
        def run():
            try:w.watch(root,['archive','live'],5,17,parallel_modes=parallel)
            except BaseException as error:errors.append(error)
        with patch.object(w,'threading',SimpleNamespace(Event=lambda:stop,RLock=threading.RLock)), \
                patch.object(stop,'wait',side_effect=wait), \
                patch.object(w.signal,'signal',side_effect=lambda sig,handler:handlers.__setitem__(sig,handler)), \
                patch.object(w,'pending_sources',side_effect=pending), \
                patch.object(w.subprocess,'Popen',side_effect=spawn):
            if threaded:
                thread=threading.Thread(target=run,daemon=True);thread.start();thread.join(timeout=5)
                self.assertFalse(thread.is_alive(),'stop during child launch must not deadlock')
            else:run()
        if errors:raise errors[0]
        return launched,checked,waits,terminated,counts

    def test_default_alternates_modes_with_one_child_at_a_time(self):
        with tempfile.TemporaryDirectory() as root:
            launched,_,_,_,counts=self.exercise(root)
        self.assertEqual(launched,['archive','live','archive','live'])
        self.assertEqual(counts['peak'],1);self.assertEqual(counts['active'],0)

    def test_failed_mode_still_gives_the_other_mode_a_turn_before_cooldown(self):
        with tempfile.TemporaryDirectory() as root:
            launched,_,waits,_,counts=self.exercise(root,results=[1,0,0,0])
        self.assertEqual(launched,['archive','live','archive','live'])
        self.assertEqual(waits,[17]);self.assertEqual(counts['peak'],1)

    def test_idle_mode_does_not_block_a_ready_mode(self):
        with tempfile.TemporaryDirectory() as root:
            launched,checked,_,_,_=self.exercise(root,ready={'live':True},stop_after=2)
        self.assertEqual(launched,['live','live'])
        self.assertEqual(checked,['archive','live','archive','live'])

    def test_both_idle_modes_wait_instead_of_spinning(self):
        with tempfile.TemporaryDirectory() as root:
            launched,checked,waits,_,_=self.exercise(root,ready={},idle=True)
        self.assertEqual(launched,[]);self.assertEqual(checked,['archive','live']);self.assertEqual(waits,[17])

    def test_stop_during_launch_terminates_the_new_child(self):
        with tempfile.TemporaryDirectory() as root:
            launched,_,_,terminated,counts=self.exercise(root,stop_after=None,stop_during_launch=True,threaded=True)
        self.assertEqual(launched,['archive']);self.assertEqual(terminated,['archive']);self.assertEqual(counts['active'],0)

    def test_parallel_modes_remain_explicitly_available(self):
        with tempfile.TemporaryDirectory() as root:
            launched,_,_,_,counts=self.exercise(root,parallel=True,stop_after=1)
        self.assertEqual(sorted(launched),['archive','live']);self.assertEqual(counts['peak'],2)
