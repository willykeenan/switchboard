import json
from pathlib import Path
import tempfile
import unittest
from activity import ActivityReader, blank, consume, present, rpc_activity, managed_activity, iso, TAIL_BYTES
NOW=2000000000

def event(kind,at=NOW,**p):return {'type':'event_msg','timestamp':iso(at),'payload':{'type':kind,**p}}
def item(kind,at=NOW,**p):return {'type':'response_item','timestamp':iso(at),'payload':{'type':kind,**p}}

class ActivityTests(unittest.TestCase):
 def observe(self,*events,at=NOW):
  s=blank()
  for e in events:consume(s,e,'codex',at)
  return present(s,at)
 def test_boundary_and_phase(self):
  a=self.observe(event('task_started',NOW-20),item('reasoning',NOW-3));self.assertEqual('thinking',a['phase']);self.assertEqual(20,a['durationSeconds']);self.assertTrue(a['active'])
 def test_completion_does_not_expire(self):
  a=self.observe(event('task_started',NOW-100),event('task_complete',NOW-70),at=NOW+1000);self.assertEqual('finished',a['phase']);self.assertFalse(a['active']);self.assertEqual(30,a['lastFinishedDurationSeconds'])
 def test_new_turn_resets_finished(self):
  a=self.observe(event('task_complete',NOW-100),event('task_started',NOW-20),item('custom_tool_call',NOW-2,name='exec',call_id='c'));self.assertEqual('tools',a['phase']);self.assertEqual(iso(NOW-100),a['lastFinishedAt'])
 def test_truncated_start_does_not_keep_old_finished(self):
  a=self.observe(event('task_complete',NOW-100),item('reasoning',NOW-2));self.assertEqual('thinking',a['phase']);self.assertIsNone(a['turnStartedAt'])
 def test_trailing_final_message_does_not_reopen(self):self.assertEqual('finished',self.observe(event('task_complete',NOW-2),item('message',NOW-1,role='assistant'))['phase'])
 def test_stale_signal(self):
  a=self.observe(item('reasoning',NOW-61));self.assertEqual('quiet',a['phase']);self.assertFalse(a['active']);self.assertEqual('thinking',a['recordedPhase'])
 def test_tool_result_stops_command_label(self):self.assertEqual('working',self.observe(item('function_call',NOW-2,name='exec_command',call_id='a'),item('function_call_output',NOW-1,call_id='a'))['phase'])
 def test_parallel_tool_pending(self):self.assertEqual('command',self.observe(item('function_call',NOW-3,name='exec_command',call_id='a'),item('function_call',NOW-2,name='web_search',call_id='b'),item('function_call_output',NOW-1,call_id='b'))['phase'])
 def test_wait_and_input_separate(self):
  self.assertEqual('waiting',self.observe(item('function_call',name='clock.sleep'))['phase']);self.assertEqual('input',self.observe(item('function_call',name='request_user_input'))['phase'])
 def test_failed_and_stopped(self):
  self.assertEqual('failed',self.observe(event('task_failed'))['phase']);self.assertEqual('stopped',self.observe(event('turn_aborted'))['phase'])
 def test_specific_tool_events(self):
  for typ,phase in [('CommandExecution','command'),('FileChange','editing'),('WebSearch','searching')]:
   self.assertEqual(phase,self.observe(event('item_started',item={'type':typ}))['phase']);self.assertEqual('working',self.observe(event('item_completed',item={'type':typ}))['phase'])
 def test_tool_failure_is_not_a_failed_turn(self):
  a=self.observe(event('item_completed',item={'type':'CommandExecution','exit_code':1}));self.assertEqual('working',a['phase']);self.assertIn('Command failed (exit 1)',a['lastAction'])
 def test_other_turn_terminal_ignored(self):self.assertEqual('starting',self.observe(event('task_started',NOW-4,turn_id='new'),event('task_complete',NOW-1,turn_id='old'))['phase'])
 def test_queue_not_execution(self):
  a=self.observe(event('user_message'));self.assertEqual('queued',a['phase']);self.assertFalse(a['active'])
 def test_bad_future_out_of_order(self):
  self.assertEqual('unknown',self.observe(item('reasoning',NOW+100),{'timestamp':iso(NOW),'type':'event_msg','payload':['bad']},None)['phase']);self.assertEqual('thinking',self.observe(item('reasoning',NOW-1),event('task_complete',NOW-20))['phase'])
 def test_privacy(self):
  a=self.observe(item('reasoning',NOW-3,summary='PRIVATE'),item('function_call',NOW-2,name='secret_tool_name',arguments='PRIVATE'),item('function_call_output',NOW-1,output='PRIVATE'));self.assertNotIn('PRIVATE',json.dumps(a));self.assertNotIn('secret_tool_name',json.dumps(a))
 def test_claude_lifecycle(self):
  s=blank()
  for e in [{'type':'assistant','timestamp':iso(NOW-3),'message':{'content':[{'type':'tool_use','id':'t','name':'Bash','input':'private'}]}},{'type':'user','timestamp':iso(NOW-2),'message':{'content':[{'type':'tool_result','tool_use_id':'t','content':'private'}]}}]:consume(s,e,'claude',NOW)
  self.assertEqual('working',present(s,NOW)['phase']);consume(s,{'type':'assistant','timestamp':iso(NOW-1),'message':{'stop_reason':'end_turn','content':[{'type':'text','text':'private'}]}},'claude',NOW);self.assertEqual('finished',present(s,NOW)['phase']);self.assertNotIn('private',json.dumps(present(s,NOW)))

class ReaderTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'log.jsonl';self.reader=ActivityReader()
 def write(self,e,mode='a'):
  with self.path.open(mode) as f:f.write(json.dumps(e)+'\n')
 def test_cache_ages_without_rereading(self):
  self.write(item('reasoning'));self.reader.observe(self.path,clock=NOW);n=self.reader.bytes_read;a=self.reader.observe(self.path,clock=NOW+61);self.assertEqual(n,self.reader.bytes_read);self.assertEqual('quiet',a['phase'])
 def test_long_transcript_bounded(self):
  self.path.write_bytes(b'x'*(TAIL_BYTES*3)+b'\n');self.write(event('task_started',NOW-2));self.write(item('reasoning',NOW-1));a=self.reader.observe(self.path,clock=NOW);self.assertLessEqual(self.reader.bytes_read,TAIL_BYTES);self.assertTrue(a['historyPartial']);self.assertEqual('thinking',a['phase'])
 def test_partial_incremental_line(self):
  raw=json.dumps(item('reasoning')).encode();self.path.write_bytes(raw[:25]);self.assertEqual('unknown',self.reader.observe(self.path,clock=NOW)['phase'])
  with self.path.open('ab') as f:f.write(raw[25:]+b'\n')
  self.assertEqual('thinking',self.reader.observe(self.path,clock=NOW)['phase'])
 def test_append_reads_only_new_bytes(self):
  self.write(event('task_started',NOW-5));self.reader.observe(self.path,clock=NOW);n=self.reader.bytes_read;size=self.path.stat().st_size;self.write(item('reasoning'));a=self.reader.observe(self.path,clock=NOW);self.assertEqual(self.path.stat().st_size-size,self.reader.bytes_read-n);self.assertEqual('thinking',a['phase'])
 def test_rotation_and_missing(self):
  self.write(item('reasoning'));self.reader.observe(self.path,clock=NOW);self.path.unlink();self.assertEqual('unavailable',self.reader.observe(self.path,clock=NOW)['phase']);self.write(event('task_complete'));self.assertEqual('finished',self.reader.observe(self.path,clock=NOW)['phase'])
 def test_truncation(self):
  self.write(item('custom_tool_call',name='exec',call_id='a'));self.reader.observe(self.path,clock=NOW);self.write(event('task_complete'),'w');self.assertEqual('finished',self.reader.observe(self.path,clock=NOW)['phase'])

class ManagedTests(unittest.TestCase):
 def test_stream_privacy(self):
  a=rpc_activity('item/reasoning/textDelta',{'delta':'PRIVATE'},NOW);self.assertEqual('thinking',a['phase']);self.assertNotIn('PRIVATE',json.dumps(a))
 def test_heartbeat_does_not_reanimate_old_reasoning(self):
  a=managed_activity({'workStatus':'RUNNING','heartbeatAt':NOW,'workActivity':rpc_activity('item/reasoning/textDelta',{},NOW-100)},NOW);self.assertEqual('working',a['phase']);self.assertIn('unavailable',a['detail'])
 def test_missing_heartbeat_uncertain(self):
  a=managed_activity({'workStatus':'RUNNING','heartbeatAt':NOW-30},NOW);self.assertEqual('uncertain',a['phase']);self.assertFalse(a['active'])
 def test_review_is_not_thinking(self):
  a=managed_activity({'workStatus':'REVIEW','workUpdatedAt':NOW,'workActivity':rpc_activity('item/reasoning/textDelta',{},NOW)},NOW);self.assertEqual('review',a['phase']);self.assertFalse(a['active'])
 def test_live_specific_tools(self):
  for typ,phase in [('commandExecution','command'),('fileChange','editing'),('webSearch','searching'),('agentMessage','responding')]:self.assertEqual(phase,rpc_activity('item/started',{'item':{'type':typ}},NOW)['phase'])
 def test_live_reasoning(self):
  a=managed_activity({'workStatus':'RUNNING','heartbeatAt':NOW,'workActivity':rpc_activity('item/reasoning/textDelta',{},NOW-1)},NOW);self.assertEqual('thinking',a['phase']);self.assertTrue(a['active'])
if __name__=='__main__':unittest.main()
