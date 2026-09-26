import unittest,tempfile,json,sqlite3,os
from pathlib import Path
from unittest.mock import patch
from inspector.transcripts import parse_event,Transcripts
from inspector.library import Library,connect,digest
from inspector.context import context,save
class Flow:
 def __init__(self,p):
  self.root=p;self.path=p/'runtime/workflow.sqlite3';self.codex_home=p/'.codex';self.codex_home.mkdir();self.log=self.codex_home/'sessions/x.jsonl';self.log.parent.mkdir();self.log.write_text('');self.s={'agent_id':'codex:exact','endpoint':'exact','provider':'codex','title':'Exact agent'};self.state={'revision':1,'placements':[{'agentId':'codex:exact','laneId':'lane','role':'researcher'}],'teams':[],'notes':[],'teamLeads':[]};self.workspace=self
  with sqlite3.connect(self.codex_home/'state_5.sqlite') as db:db.execute('CREATE TABLE threads(id,rollout_path)');db.execute('INSERT INTO threads VALUES(?,?)',('exact',str(self.log)))
 def catalog(self):return {'sessions':[self.s]}
 def read(self):return {**self.state,'lanes':[{'id':'lane','projectId':'project','name':'Lane','objective':'Original question'}]}
 def snapshot(self):return {**self.read(),'projects':[{'id':'project','name':'Project'}]}
class Tests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name).resolve();self.flow=Flow(self.p);self.lib=Library(self.p,self.flow,self.p/'library');self.lib.last_start=__import__('time').time()
 def tearDown(self):self.tmp.cleanup()
 def event(self,text,role='assistant',channel='final'):return {'type':'response_item','payload':{'type':'message','role':role,'channel':channel,'content':[{'type':'output_text','text':text}]}}
 def test_public_only(self):
  for role,channel in [('assistant','analysis'),('assistant','summary'),('system','final'),('developer','final')]:self.assertEqual(parse_event(self.event('INTERNAL',role,channel),'codex'),[])
  self.assertEqual(parse_event(self.event('Public'),'codex')[0]['text'],'Public')
  self.assertEqual(parse_event({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'PRIVATE'},{'type':'text','text':'Public'}]}},'claude')[0]['text'],'Public')
 def test_identity_symlinks_and_partial_log(self):
  self.flow.log.write_text(json.dumps(self.event('hello'))+'\n'+json.dumps(self.event('partial')));t=Transcripts(self.flow);self.assertEqual([e['text'] for e in t.page('codex:exact')['entries']],['hello']);self.assertTrue(t.page('codex:exact')['partialTail'])
  with self.assertRaises(ValueError):t.page('codex:wrong')
  self.flow.log.unlink();self.flow.log.symlink_to(self.p/'outside')
  with self.assertRaises(ValueError):t.page('codex:exact')
 def test_pagination(self):
  self.flow.log.write_text(''.join(json.dumps(self.event(str(i)))+'\n' for i in range(220)));t=Transcripts(self.flow);a=t.page('codex:exact');b=t.page('codex:exact',a['nextBefore']);c=t.page('codex:exact',b['nextBefore']);self.assertEqual([e['text'] for e in c['entries']+b['entries']+a['entries']],[str(i) for i in range(220)])
 def test_notes_immediately_searchable_and_idempotent(self):
  item={'project':'project','lane':'lane','title':'Observation','body':'Unique result quartz','requestId':'exact-note'};a=self.lib.write('note',item);b=self.lib.write('note',item);self.assertEqual(a,b);self.assertEqual(self.lib.query('project',q='quartz')['total'],1)
  with self.assertRaises(ValueError):self.lib.write('note',{**item,'body':'Changed'})
 def test_context_versions_and_conflict(self):
  save(self.lib,{'project':'project','lane':'lane','version':0,'question':'Which evidence exists?'})
  c=context(self.lib,'project','lane');self.assertEqual(c['current']['version'],1)
  with self.assertRaises(ValueError):save(self.lib,{'project':'project','lane':'lane','version':0,'question':'Overwritten'})
  self.assertEqual(self.lib.query('project',kind='Team context')['total'],1)
 def test_review_is_bound_to_current_team_context(self):
  c=context(self.lib,'project','lane');item={'project':'project','lane':'lane','decision':'New research','reason':'No existing evidence','contextFingerprint':c['fingerprint'],'requestId':'r1'};a=self.lib.write('review',item);self.assertTrue(context(self.lib,'project','lane')['reviews'][0]['contextCurrent'])
  self.flow.state['notes']=[{'agentId':'codex:exact','text':'Changed scope'}]
  self.assertFalse(context(self.lib,'project','lane')['reviews'][0]['contextCurrent'])
  with self.assertRaises(ValueError):self.lib.write('review',{**item,'requestId':'r2'})
 def test_invalid_team_and_cross_project(self):
  with self.assertRaises(ValueError):context(self.lib,'wrong','lane')
  with self.assertRaises(ValueError):context(self.lib,'project','lane','imaginary-team')
 def test_changed_source_blocks_review(self):
  f=self.p/'evidence.md';f.write_text('original');st=f.stat();sha=digest('original')
  with connect(self.lib.dbpath) as db:db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('evidence','project','lane',str(f),'Evidence','Research',sha,'original',st.st_mtime,st.st_size,'now','Indexed','Test'))
  c=context(self.lib,'project','lane');f.write_text('revised source')
  with self.assertRaises(ValueError):self.lib.write('review',{'project':'project','lane':'lane','decision':'Reuse','reason':'Reuse source','contextFingerprint':c['fingerprint'],'items':['evidence']})
  self.assertEqual(self.lib.item('evidence')['state'],'Source changed')
if __name__=='__main__':unittest.main(verbosity=2)
