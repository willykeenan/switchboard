import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agents.observer import Observer, epoch, iso, progress_view, read_events, session_activity


class EventsTests(unittest.TestCase):
    def test_thinking_is_fresh_event_not_cpu(self):
        now=time.time()
        events=[{'type':'response_item','timestamp':iso(now-8),'payload':{'type':'reasoning'}}]
        self.assertEqual(session_activity(events,now,'Codex')['activity'],'thinking')
        self.assertEqual(session_activity(events,now+100,'Codex')['activity'],'unknown')

    def test_completion_is_not_working(self):
        now=time.time()
        events=[{'type':'event_msg','timestamp':iso(now-2),'payload':{'type':'task_complete'}},
                {'type':'response_item','timestamp':iso(now-1),'payload':{'type':'message','role':'assistant'}}]
        self.assertEqual(session_activity(events,now,'Codex')['activity'],'completed')

    def test_next_turn_resets_completion(self):
        now=time.time()
        events=[{'type':'event_msg','timestamp':iso(now-3),'payload':{'type':'task_complete'}},
                {'type':'event_msg','timestamp':iso(now-2),'payload':{'type':'task_started'}},
                {'type':'response_item','timestamp':iso(now-1),'payload':{'type':'function_call'}}]
        self.assertEqual(session_activity(events,now,'Codex')['activity'],'tool activity')

    def test_future_and_broken_events_are_unknown(self):
        self.assertIsNone(epoch('broken'))
        event={'type':'response_item','timestamp':iso(time.time()+1000),'payload':{'type':'reasoning'}}
        self.assertEqual(session_activity([event],time.time(),'Codex')['activity'],'unknown')

    def test_claude_end_turn(self):
        event={'type':'assistant','timestamp':iso(),'message':{'stop_reason':'end_turn','content':[{'type':'text','text':'private response'}]}}
        result=session_activity([event],time.time(),'Claude')
        self.assertEqual(result['activity'],'completed')
        self.assertNotIn('private response',json.dumps(result))

    def test_partial_tail(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.jsonl';p.write_text('broken\n{"type":"ok"}\n{"unfinished":')
            self.assertEqual(read_events(p),[{'type':'ok'}])

    def test_malformed_provider_metadata_does_not_fail_collector(self):
        now=time.time()
        self.assertEqual(session_activity([{'type':'event_msg','timestamp':iso(),'payload':[]}],now,'Codex')['activity'],'unknown')
        self.assertEqual(session_activity([{'type':'assistant','timestamp':iso(),'message':'invalid'}],now,'Claude')['activity'],'unknown')


class ProgressTests(unittest.TestCase):
    def test_published_progress(self):
        self.assertEqual(progress_view({'available':True,'processed':2,'total':4,'observedAt':iso()},time.time())['percent'],50)

    def test_unavailable_stale_zero_and_nonfinite(self):
        for p in [dict(available=True,processed=2,total=0),dict(available=True,processed=float('nan'),total=4),
                  dict(available=True,processed=2,total=4,updatedAt=iso(time.time()-700)),
                  dict(available=False,processed=2,total=4,updatedAt=iso())]:
            self.assertFalse(progress_view(p,time.time())['available'])


class ProcessTests(unittest.TestCase):
    def process(self,pid=2,start=10,ticks=1,ppid=1,args=None):
        class Process:
            def as_dict(self,**kwargs):
                from types import SimpleNamespace as S
                return dict(pid=pid,ppid=ppid,name='node',cmdline=args or ['node'],create_time=start,
                            cpu_times=S(user=ticks,system=0),memory_info=S(rss=42),num_threads=1,
                            status='sleeping',username='test',uids=S(real=os.getuid()))
        return Process()

    def _psutil(self, processes):
        class Dummy:
            NoSuchProcess = type('NoSuchProcess', (Exception,), {})
            AccessDenied = type('AccessDenied', (Exception,), {})
            @staticmethod
            def process_iter():
                return processes
        return Dummy

    def test_pid_reuse_does_not_inherit_cpu(self):
        o=Observer()
        with patch('agents.observer.psutil', self._psutil([self.process()])):
            rows,_=o.processes(1)
        self.assertIsNone(rows[0]['cpuPercent'])
        with patch('agents.observer.psutil', self._psutil([self.process(ticks=2)])):
            rows,_=o.processes(3)
        self.assertEqual(rows[0]['cpuPercent'],50)
        with patch('agents.observer.psutil', self._psutil([self.process(start=20,ticks=1)])):
            rows,_=o.processes(5)
        self.assertIsNone(rows[0]['cpuPercent'])

    def test_commands_and_prompt_provider_names_are_not_exposed(self):
        o=Observer()
        with patch('agents.observer.psutil', self._psutil([self.process(args=['node','--token','PRIVATE_KEY','--prompt','codex claude'])])):
            rows,_=o.processes(1)
        self.assertIsNone(rows[0]['provider'])
        self.assertNotIn('PRIVATE_KEY',json.dumps(rows))
        self.assertNotIn('--prompt',json.dumps(rows))

    def test_runtime_descendant_relationship(self):
        o=Observer()
        with patch('agents.observer.psutil', self._psutil([self.process(args=['/app/Codex']),self.process(pid=3,ppid=2)])):
            rows,_=o.processes(1)
        child=next(r for r in rows if r['pid']==3)
        self.assertEqual(child['provider'],'Codex')
        self.assertEqual(child['category'],'Runtime descendant')


SNAPSHOT = Path(__file__).resolve().parents[1] / 'worker_snapshot.mjs'


@unittest.skipUnless(SNAPSHOT.is_file(), 'worker snapshot adapter is not bundled')
class AdapterTests(unittest.TestCase):
    def test_pid_one_never_adopts_unrelated_workers(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'gpu').mkdir();(root/'cpu').mkdir()
            spec={'schemaVersion':'ke.cpu-job.v2','jobId':'fixture','title':'Own worker','parentPid':1,'workers':[{'pid':100,'id':'own'}]}
            (root/'cpu/job.json').write_text(json.dumps(spec))
            rows=[dict(pid=p,ppid=1,uid=os.getuid(),executable='python3',cpuPercent=4,state='R',rssBytes=1024,elapsedSeconds=10) for p in [1,100,200,300]]
            env={**os.environ,'KE_CPU_JOBS_ROOT':str(root/'cpu'),'KE_GPU_JOBS_ROOT':str(root/'gpu')}
            r=subprocess.run(['node',str(Path(__file__).resolve().parents[1]/'worker_snapshot.mjs')],input=json.dumps(rows),text=True,capture_output=True,env=env,check=True,timeout=6)
            s=json.loads(r.stdout)
            self.assertTrue(s['cpu']['ok'])
            self.assertEqual([w['pid'] for w in s['cpu']['snapshot']['pools'][0]['workers']],[100])
            self.assertEqual(json.loads((root/'cpu/job.json').read_text()),spec)

    def test_shared_app_parent_and_reused_pid_are_not_job_workers(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'gpu').mkdir();(root/'cpu').mkdir()
            spec={'schemaVersion':'ke.cpu-job.v2','jobId':'fixture','title':'Old job','parentPid':339,'registeredAt':iso(time.time()-3600),'workers':[{'pid':100,'id':'own'}]}
            (root/'cpu/job.json').write_text(json.dumps(spec))
            rows=[dict(pid=p,ppid=339,uid=os.getuid(),executable='python3',cpuPercent=4,state='R',rssBytes=1024,elapsedSeconds=10,startedAt=iso(time.time()-10)) for p in [100,200,300]]
            rows.append(dict(pid=339,ppid=1,uid=os.getuid(),executable='codex',state='S',cpuPercent=0))
            env={**os.environ,'KE_CPU_JOBS_ROOT':str(root/'cpu'),'KE_GPU_JOBS_ROOT':str(root/'gpu')}
            r=subprocess.run(['node',str(Path(__file__).resolve().parents[1]/'worker_snapshot.mjs')],input=json.dumps(rows),text=True,capture_output=True,env=env,check=True,timeout=6)
            pools=json.loads(r.stdout)['cpu']['snapshot']['pools']
            registered=next(p for p in pools if p['registered'])
            self.assertEqual([w['pid'] for w in registered['workers']],[100])
            self.assertFalse(registered['workers'][0]['processAlive'])


if __name__=='__main__':unittest.main()
