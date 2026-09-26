import copy
import json
import sqlite3
from unittest.mock import patch
import unittest
import test_workflow
from workflow import DEFAULT, Workflow, allowed
from workspace import Conflict

class LayoutTests(unittest.TestCase):
    setUp=test_workflow.WorkflowTests.setUp
    tearDown=test_workflow.WorkflowTests.tearDown
    def edit(self,op,**item):
        return self.flow.mutate({'operation':op,'revision':self.flow.read()['revision'],'item':item})
    def team(self,id='research-pod',parent='researcher'):
        return self.edit('team',id=id,laneId='alpha',role='researcher',parentId=parent,name='Research pod',objective='Compare evidence')
    def test_multiple_members_and_lead_survive_reload(self):
        for aid in ('codex:a','codex:review'):
            self.edit('place',agentId=aid,laneId='alpha',role='researcher')
        self.edit('team-lead',laneId='alpha',teamId='researcher',agentId='codex:a')
        c=Workflow(self.root,self.w,self.flow.codex_home).context('a')
        self.assertEqual(2,len(c['lane']['members']));self.assertEqual('codex:a',c['lane']['teamLeads'][0]['agentId'])
        self.edit('place',agentId='codex:a',laneId='beta',role='writer')
        self.assertEqual([],self.flow.read()['teamLeads'])
    def test_nested_teams_and_cycle_rejection(self):
        self.team();self.team('sources','research-pod')
        self.edit('place',agentId='codex:a',laneId='alpha',role='researcher',teamId='sources')
        self.assertEqual('sources',self.flow.context('a')['lane']['yourTeam'])
        before=self.flow.read()
        with self.assertRaises(ValueError):self.team('research-pod','sources')
        self.assertEqual(before,self.flow.read())
        with self.assertRaises(ValueError):self.edit('place',agentId='codex:b',laneId='beta',role='researcher',teamId='sources')
    def test_dissolve_team_keeps_children_and_members(self):
        self.team();self.team('sources','research-pod')
        self.edit('place',agentId='codex:a',laneId='alpha',role='researcher',teamId='research-pod')
        self.edit('team-remove',teamId='research-pod')
        state=self.flow.read();self.assertEqual('researcher',state['teams'][0]['parentId']);self.assertNotIn('teamId',next(p for p in state['placements'] if p['agentId']=='codex:a'))
    def test_lane_create_and_undo_share_one_revision(self):
        old=self.w.path.read_bytes();self.edit('lane-create',id='new-lane',projectId='demo',name='New lane',objective='Bounded work')
        self.assertIn('new-lane',[l['id'] for l in self.w.read()['lanes']])
        self.assertEqual(old,self.w.path.read_bytes())
        self.edit('undo');self.assertNotIn('new-lane',[l['id'] for l in self.w.read()['lanes']])
        with self.assertRaises(Conflict):self.flow.mutate({'operation':'lane-create','revision':1,'item':dict(id='stale',projectId='demo',name='Stale',objective='Stale')})
    def test_closed_lane_holds_even_explicit_messages_and_reopen_restores(self):
        self.edit('connection',**{'from':'codex:a','to':'codex:b','allow':True})
        self.flow.send('codex:a','codex:b','Evidence','closed-test')
        before=self.flow.read()['placements'];self.edit('lane-close',laneId='alpha')
        self.assertFalse(allowed(self.flow.read(),'codex:a','codex:b'));self.assertEqual('HELD',self.flow.messages()[0]['status']);self.assertEqual(before,self.flow.read()['placements'])
        with self.assertRaises(ValueError):self.edit('place',agentId='codex:b',laneId='alpha',role='researcher')
        self.edit('lane-reopen',laneId='alpha');self.assertTrue(allowed(self.flow.read(),'codex:a','codex:b'))
    def test_merge_retains_team_tree_leads_block_and_history_then_undo(self):
        self.team();self.edit('place',agentId='codex:a',laneId='alpha',role='researcher')
        self.edit('team-lead',laneId='alpha',teamId='researcher',agentId='codex:a')
        self.edit('connection',**{'from':'codex:a','to':'codex:b','allow':False})
        before=self.flow.read();board=(self.root/'board.sqlite3').read_bytes()
        self.edit('lane-merge',laneId='alpha',targetId='beta');s=self.flow.read()
        self.assertEqual('merged',next(l for l in s['laneCatalog'] if l['id']=='alpha')['lifecycle'])
        self.assertTrue(all(p['laneId']=='beta' for p in s['placements']))
        lead=s['teamLeads'][0];self.assertEqual('beta',lead['laneId']);self.assertNotEqual('researcher',lead['teamId'])
        self.assertFalse(allowed(s,'codex:a','codex:b'));self.assertEqual(board,(self.root/'board.sqlite3').read_bytes())
        self.edit('undo');after=self.flow.read();before.pop('revision');after.pop('revision');self.assertEqual(before,after)
    def test_alignment_context_requires_human_confirmation(self):
        self.edit('lane-edit',laneId='alpha',name='Alignment & Audit',kind='support',supportMode='alignment')
        c=self.flow.context('a')['lane'];self.assertEqual('advisory',c['charter']['mode']);self.assertTrue(c['charter']['humanConfirmationRequired']);self.assertIn('operator acknowledgment',c['charter']['decisionRule'])
    def test_legacy_lane_editor_cannot_bypass_history(self):
        with self.assertRaises(Conflict):self.w.mutate({'operation':'lane','revision':self.w.read()['revision'],'item':{}})
    def test_atomic_invalid_merge_and_position_rejected(self):
        before=self.flow.read()
        with self.assertRaises(ValueError):self.edit('lane-merge',laneId='alpha',targetId='alpha')
        with self.assertRaises(ValueError):self.edit('team-position',laneId='alpha',teamId='researcher',x=float('nan'),y=1)
        self.assertEqual(before,self.flow.read())
    def test_old_history_defaults_upgrade_on_read_and_undo(self):
        with sqlite3.connect(self.flow.path) as db:
            row=json.loads(db.execute('SELECT body FROM workflow WHERE id=1').fetchone()[0])
            for key in ('laneCatalog','teams','teamLeads','teamPositions'):row.pop(key)
            db.execute('UPDATE workflow SET body=?',(json.dumps(row),));db.execute('UPDATE workflow_history SET body=? WHERE revision=1',(json.dumps(row),))
        self.edit('lane-close',laneId='alpha');self.edit('undo');self.assertIsNone(self.flow.read()['laneCatalog'])

    def test_backup_restores_nested_layout_and_rejects_corruption_without_live_writes(self):
        from workflow_layout import snapshot_stores, restore_stores
        self.team();self.edit('place',agentId='codex:a',laneId='alpha',role='researcher',teamId='research-pod')
        before=self.flow.path.read_bytes();snapshot=snapshot_stores(self.root)
        result=restore_stores(self.root.parent/'restored',snapshot)
        self.assertEqual(2,result['files']);self.assertEqual(before,self.flow.path.read_bytes())
        with sqlite3.connect(self.root.parent/'restored/runtime/workflow.sqlite3') as db:
            restored=json.loads(db.execute('SELECT body FROM workflow').fetchone()[0])
        self.assertEqual(self.flow.read()['teams'],restored['teams'])
        snapshot['files'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):restore_stores(self.root.parent/'corrupt-restore',snapshot)
        with self.assertRaises(FileExistsError):restore_stores(self.root,snapshot)
    def test_lead_must_be_direct_member(self):
        with self.assertRaises(ValueError):self.edit('team-lead',laneId='alpha',teamId='researcher',agentId='codex:b')
