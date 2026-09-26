"""Durable exact-recipient handoffs. A wake is one existing-provider turn, not a hire.

The daemon owns provider writes. Callers only enqueue under the saved graph.
Unknown delivery never retries. Notifications and pre-install history never wake.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from workflow import allowed, now

ACTIVE = ('SENDING', 'UNCERTAIN', 'ACCEPTED', 'RUNNING')
PENDING = ('WAITING', 'BUSY', 'HELD', 'UNAVAILABLE', 'MANAGED_POLICY', 'OWNER_REJECTED')
TERMINAL = ('RETURNED', 'FAILED', 'ACKNOWLEDGED', 'CANCELLED', 'DUPLICATE')
SCAN_ROWS = 8
SCAN_SECONDS = 10
ENROLL_ROWS = 16
ENROLL_SECONDS = 2
ABSENT_SCANS = 3            # complete history scans with zero matching turns
ABSENT_SECONDS = 3600      # ...spanning at least this long before an UNCERTAIN row is released
RESEND_MAX_FAILED = 2      # automatic resends after a delivered turn errored
RESEND_MAX_STOPPED = 1     # ...after a delivered turn was interrupted
RESEND_STOPPED_WAIT = 600  # seconds an interrupted turn waits before its one resend
RESEND_WINDOW = 86400      # only failures from the last day are reviewed
RESEND_KEYS = ('resends','resentAfter','previousRequests')

PROVIDER_STATUS_URL = 'https://status.openai.com/api/v2/summary.json'
PROVIDER_DOWN = ('major_outage', 'full_outage')
# Components that carry desktop/CLI Codex traffic. Codex Web alone does not hold delivery.
CODEX_COMPONENTS = ('Codex API', 'CLI', 'VS Code extension')
_provider_cache = {'at': 0.0, 'value': None}

def _fetch_provider_status():
    import urllib.request
    with urllib.request.urlopen(PROVIDER_STATUS_URL, timeout=5) as response:
        return json.loads(response.read(500000))

CODEX_LOGS = Path.home()/'.codex/logs_2.sqlite'
RECOVERED_INCIDENT = ('monitoring', 'resolved', 'postmortem')

def _recent_local_codex_success(seconds=300):
    """True when this Mac's Codex got a successful model response recently.

    Status pages lag real recovery; one observed 200 on /responses is direct
    evidence. Any read failure returns False (the status page then decides).
    """
    try:
        db=sqlite3.connect(CODEX_LOGS.as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            row=db.execute("SELECT 1 FROM logs WHERE ts>? AND feedback_log_body LIKE '%api.path=\"/responses\"%' "
                           "AND feedback_log_body LIKE '%status=200%' LIMIT 1",(int(time.time())-seconds,)).fetchone()
        finally:db.close()
        return bool(row)
    except Exception:
        return False

def codex_provider_outage(clock=time.time, fetch=None, local_success=None):
    """Short reason while a Codex outage is still in effect, else None. Cached 60 s.

    Hold only when all three agree: status.openai.com shows a Codex component at
    major/full outage, no Codex incident has reached monitoring/resolved, and this
    Mac has not seen a successful Codex response in the last 5 minutes. Unknown
    (network or parse failure) returns None: the guard fails open.
    """
    at=clock()
    if at-_provider_cache['at']<60:return _provider_cache['value']
    value=None
    try:
        raw=(fetch or _fetch_provider_status)()
        status={c.get('name'):c.get('status') for c in raw.get('components',[])}
        down=[name for name in CODEX_COMPONENTS if status.get(name) in PROVIDER_DOWN]
        incidents=[i for i in raw.get('incidents',[]) if 'codex' in str(i.get('name','')).lower()]
        recovering=bool(incidents) and all(str(i.get('status','')).lower() in RECOVERED_INCIDENT for i in incidents)
        if down and not recovering and not (local_success or _recent_local_codex_success)():
            value=('Held: OpenAI reports a Codex outage ('+', '.join(down)+'). No attempt used; '
                   'delivery resumes automatically when OpenAI recovers.')
    except Exception:
        value=None
    _provider_cache.update(at=at,value=value)
    return value

def atomic_json(path, value):
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)

class Handoffs:
    def __init__(self, flow, transport=None, clock=time.time, monotonic=time.monotonic):
        self.flow=flow;self.root=flow.root;self.clock=clock;self.transport=transport;self.monotonic=monotonic
        self.path=self.root/'runtime/workflow-handoffs.sqlite3'
        self.policy_path=self.root/'runtime/workflow-handoffs-policy.json'
        self.health_path=self.root/'runtime/workflow-handoffs-health.json'
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise ValueError('Handoff database cannot be a symlink')
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('''CREATE TABLE IF NOT EXISTS handoffs(
          id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
          request_id TEXT NOT NULL UNIQUE, body_sha TEXT NOT NULL, target TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL, updated REAL NOT NULL, next_check REAL NOT NULL DEFAULT 0,
          detail TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '{}')''')
        db.execute('''CREATE TABLE IF NOT EXISTS events(
          sequence INTEGER PRIMARY KEY, handoff_id TEXT, at REAL, status TEXT, detail TEXT)''')
        db.execute('CREATE TABLE IF NOT EXISTS handoff_scan_cursors(kind TEXT PRIMARY KEY,created,id TEXT NOT NULL)')
        self.path.chmod(0o600);return db
    def policy(self):
        try:return json.loads(self.policy_path.read_text())
        except (OSError,ValueError):return {'enabled':False}
    def turn_still_open(self,h,obs):
        activity=self.flow.activity_snapshot([h['recipient']])['activities'].get(h['recipient']) or {}
        turn=(obs.get('receipt') or {}).get('turnId')
        return activity.get('turnStatus')=='open' and (not activity.get('turnId') or not turn or activity.get('turnId')==turn)
    def work_open(self,mid):
        """True while the delivered work still needs doing.

        With a work contract: open until returned or closed (accepting or reading it
        does not finish it). A plain message: open until read-acknowledged.
        """
        m=self.message(mid)
        from workflow import workflow_reader
        with workflow_reader(self.flow.path) as db:
            db.row_factory=sqlite3.Row
            table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_work'").fetchone()
            row=db.execute('SELECT returned,closed FROM workflow_work WHERE message_id=?',(mid,)).fetchone() if table else None
        if row:return not row['returned'] and not row['closed']
        return not m.get('read_at')
    def review_failures(self):
        """Resend delivered-but-failed handoffs automatically.

        A FAILED row is re-observed first: a turn that actually finished becomes
        RETURNED and nothing is sent. Errors are resent up to RESEND_MAX_FAILED
        times; an interrupted turn once, after RESEND_STOPPED_WAIT, and only if
        the work is untouched. A resend is a fresh delivery of the same message
        with a new request ID; the earlier turn is never replayed or edited.
        """
        at=self.clock();reviewed=0
        with self.connect() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM handoffs WHERE status='FAILED' AND updated>=? AND receipt NOT LIKE '%\"resendDecision\"%' "
                                                "ORDER BY updated LIMIT 8",(at-RESEND_WINDOW,))]
        for h in rows:
            receipt=json.loads(h['receipt'])
            if receipt.get('resendDecision'):continue
            target=json.loads(h['target'])
            try:obs=self.transport.observe(target,h['request_id'],receipt)
            except Exception:continue
            if obs.get('status') in ('RETURNED','RUNNING') or (obs.get('status')=='FAILED' and self.turn_still_open(h,obs)):
                status=obs['status'] if obs.get('status')!='FAILED' else 'RUNNING'
                self.update(h['id'],status,'Corrected: the delivered turn did not fail ('+status.lower()+').',{**receipt,**obs.get('receipt',{})});reviewed+=1;continue
            activity=self.flow.activity_snapshot([h['recipient']])['activities'].get(h['recipient']) or {}
            stopped=activity.get('turnStatus')=='stopped' and activity.get('turnId')==receipt.get('turnId')
            kind='interrupted' if stopped else 'failed'
            done=int(receipt.get('resends') or 0)
            limit=RESEND_MAX_STOPPED if stopped else RESEND_MAX_FAILED
            if stopped and at-h['updated']<RESEND_STOPPED_WAIT:continue
            if done>=limit or not self.work_open(h['id']):
                why='resend limit reached' if done>=limit else 'the work is already returned, closed or acknowledged'
                self.update(h['id'],'FAILED','Delivered turn '+kind+'; not resent: '+why+'.',{**receipt,'resendDecision':'final'});reviewed+=1;continue
            fresh=str(uuid.uuid4())
            history=receipt.get('previousRequests') or []
            new_receipt={'resends':done+1,'resentAfter':kind,'previousRequests':history+[{'requestId':h['request_id'],'turnId':receipt.get('turnId')}]}
            detail='Resending automatically after the delivered turn '+kind+' ('+str(done+1)+'/'+str(limit)+'). The earlier turn is not replayed.'
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                cur=db.execute("UPDATE handoffs SET status='WAITING',attempts=0,request_id=?,receipt=?,detail=?,updated=?,next_check=? WHERE id=? AND status='FAILED'",
                               (fresh,json.dumps(new_receipt),detail,at,at+30,h['id']))
                if cur.rowcount:db.execute('INSERT INTO events(handoff_id,at,status,detail) VALUES(?,?,?,?)',(h['id'],at,'WAITING',detail))
            reviewed+=1
        return reviewed
    @staticmethod
    def proven_absent(obs):
        """A complete marker-based history scan (no trusted turn id) found zero candidates."""
        search=(obs.get('receipt') or {}).get('historySearch') or {}
        return (not obs.get('status') and search.get('exhausted') is True and search.get('candidate') is None
                and not search.get('turnId'))
    def provider_outage(self):
        """Opt-in via policy key providerOutageGuard; tests and old policies are unaffected."""
        return codex_provider_outage() if self.policy().get('providerOutageGuard') else None
    def enable(self,authority):
        if not authority.strip():raise ValueError('Record the actual user authorization')
        old=self.policy()
        p={**old,'enabled':True,'newMessagesAfter':old.get('newMessagesAfter',now()),
           'authority':authority,'updatedAt':now(),'interruptBusy':False,'historicalMassWake':False}
        atomic_json(self.policy_path,p);return p
    def snapshot(self):
        try:
            with self.connect() as db: rows=[dict(x) for x in db.execute('SELECT * FROM handoffs ORDER BY created DESC LIMIT 200')]
            for row in rows:
                row['target']=json.loads(row['target']);row['receipt']=json.loads(row['receipt'])
                row['needsAttention']=row['status'] in ('HELD','UNAVAILABLE','OWNER_REJECTED','MANAGED_POLICY','UNCERTAIN','FAILED') or (row['status']=='SENDING' and self.clock()-row['updated']>60) or (row['status'] in ('WAITING','BUSY') and self.clock()-row['created']>300)
            try:health=json.loads(self.health_path.read_text())
            except (OSError,ValueError):health={}
            fresh=bool(health.get('at') and self.clock()-health['at']<45)
            return {'enabled':self.policy().get('enabled',False),'daemonHealthy':fresh and health.get('ok',False),
                    'health':health,'deliveryHealthy':not any(r['status'] in ('UNAVAILABLE','OWNER_REJECTED','UNCERTAIN','SENDING') for r in rows),
                    'unavailableRecipients':len({r['recipient'] for r in rows if r['status']=='UNAVAILABLE'}),
                    'busyRecipients':len({r['recipient'] for r in rows if r['status']=='BUSY'}),
                    'handoffs':rows,'attention':sum(r['needsAttention'] for r in rows)}
        except (OSError,ValueError,sqlite3.Error) as e:return {'enabled':False,'daemonHealthy':False,'error':str(e),'handoffs':[]}
    def message(self,mid):
        with self.flow._connect() as db:
            row=db.execute('SELECT * FROM workflow_messages WHERE id=?',(mid,)).fetchone()
        if not row:raise ValueError('Unknown original workflow message')
        return dict(row)
    def enroll(self,mid):
        m=self.message(mid)
        target=next((s for s in self.flow.identity_catalog()['sessions'] if s['agent_id']==m['recipient']),None)
        if not target:raise ValueError('Exact recipient is no longer registered')
        t={k:target.get(k) for k in ('agent_id','endpoint','provider','title','display_name','cwd','managed')}
        digest=hashlib.sha256(m['body'].encode()).hexdigest();at=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM handoffs WHERE id=?',(mid,)).fetchone()
            if existing:
                if (existing['sender'],existing['recipient'],existing['body_sha'])!=(m['sender'],m['recipient'],digest):raise ValueError('Handoff identity changed')
            else:
                duplicate=db.execute("SELECT id FROM handoffs WHERE sender=? AND recipient=? AND body_sha=? AND status!='CANCELLED' LIMIT 1",(m['sender'],m['recipient'],digest)).fetchone()
                status='DUPLICATE' if duplicate else 'WAITING'
                db.execute('INSERT INTO handoffs(id,sender,recipient,request_id,body_sha,target,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                  (mid,m['sender'],m['recipient'],str(uuid.uuid4()),digest,json.dumps(t),status,at,at))
                db.execute('INSERT INTO events(handoff_id,at,status,detail) VALUES(?,?,?,?)',(mid,at,status,'Repeated identical action suppressed.' if duplicate else 'Exact original message enrolled; no provider write yet.'))
        return self.get(mid)
    def get(self,mid):
        with self.connect() as db: return dict(db.execute('SELECT * FROM handoffs WHERE id=?',(mid,)).fetchone())
    def update(self,mid,status,detail,receipt=None,delay=10):
        at=self.clock()
        with self.connect() as db:
            previous=db.execute('SELECT status,detail FROM handoffs WHERE id=?',(mid,)).fetchone()
            db.execute('UPDATE handoffs SET status=?,detail=?,updated=?,next_check=? WHERE id=?',(status,detail,at,at+delay,mid))
            if receipt is not None:
                # Resend history must survive later receipts (send result, observation) or the cap could be bypassed.
                kept=json.loads((db.execute('SELECT receipt FROM handoffs WHERE id=?',(mid,)).fetchone() or ['{}'])[0] or '{}')
                receipt={**{k:kept[k] for k in RESEND_KEYS if k in kept},**receipt}
                db.execute('UPDATE handoffs SET receipt=? WHERE id=?',(json.dumps(receipt),mid))
            if not previous or tuple(previous)!=(status,detail):db.execute('INSERT INTO events(handoff_id,at,status,detail) VALUES(?,?,?,?)',(mid,at,status,detail))
    def scan_cursor(self,kind):
        with self.connect() as db:
            row=db.execute('SELECT created,id FROM handoff_scan_cursors WHERE kind=?',(kind,)).fetchone()
        return (row[0],int(row[1])) if row else None
    def advance_scan(self,kind,created,mid):
        # Scheduling only: never changes a work record, provider cursor or receipt.
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO handoff_scan_cursors VALUES(?,?,?)',(kind,created,mid))
    def reset_scan(self,kind):
        with self.connect() as db:db.execute('DELETE FROM handoff_scan_cursors WHERE kind=?',(kind,))
    def scan(self):
        p=self.policy()
        if not p.get('enabled'):return {'checked':0,'errors':[]}
        cursor=self.scan_cursor('enrollment');start=self.monotonic()
        with self.flow._connect() as db:
            modes=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_message_intents'").fetchone()
            query='SELECT m.rowid AS scan_sequence,m.id,m.created_at FROM workflow_messages m'
            if modes:query+=' LEFT JOIN workflow_message_intents i ON m.id=i.message_id'
            query+=' WHERE m.created_at>=? AND m.read_at IS NULL'
            if modes:query+=" AND COALESCE(i.intent,'handoff')='handoff'"
            after=' AND (m.created_at>? OR (m.created_at=? AND m.rowid>?))'
            rows=db.execute(query+(after if cursor else '')+' ORDER BY m.created_at,m.rowid LIMIT ?',
                (p['newMessagesAfter'],)+((cursor[0],cursor[0],cursor[1]) if cursor else ())+ (ENROLL_ROWS,)).fetchall()
            if not rows and cursor:
                rows=db.execute(query+' ORDER BY m.created_at,m.rowid LIMIT ?',(p['newMessagesAfter'],ENROLL_ROWS)).fetchall()
        checked=0;errors=[]
        for row in rows:
            if checked and self.monotonic()-start>=ENROLL_SECONDS:break
            with self.connect() as db:exists=db.execute('SELECT 1 FROM handoffs WHERE id=?',(row['id'],)).fetchone()
            if not exists:
                try:self.enroll(row['id'])
                except ValueError as e:errors.append({'messageId':row['id'],'error':str(e)[:300]})
            self.advance_scan('enrollment',row['created_at'],row['scan_sequence']);checked+=1
        if checked==len(rows) and len(rows)<ENROLL_ROWS:self.reset_scan('enrollment')
        return {'checked':checked,'errors':errors}
    def envelope(self,h,m,target):
        from workflow_work import envelope
        from workflow_handoff_presentation import card, context, DETAILS
        recorded=context(self.flow,h['id'])
        work=envelope(self.flow,h['id'])
        # Readable card first; the exact delivery marker (HANDOFF/Exact sender) opens the
        # technical block that marker_matches checks. Old v1 cards still match.
        return (card(self.flow,h['sender'],h['recipient'],body=m['body'],**recorded)+DETAILS+
          f"HANDOFF {h['request_id']}\nExact sender: {h['sender']}\nExact recipient: {h['recipient']}\nOriginal workflow message: {h['id']}\n\n"
          "Take the next authorized step for your existing role and task, then return evidence or the exact blocker. "
          "No acknowledgment-only replies; read-ack or ending your turn does not close work."+work)
    def active_blocker(self,db,h):
        return db.execute('SELECT id,status,detail FROM handoffs WHERE recipient=? AND id!=? AND status IN (?,?,?,?) ORDER BY created,id LIMIT 1',
                          (h['recipient'],h['id'],*ACTIVE)).fetchone()
    def block_pending(self,db,h,blocker):
        # Called under the same reservation lock as admission. Never changes
        # attempts, request identity, provider receipt or the blocking row.
        previous=db.execute('SELECT status,detail FROM handoffs WHERE id=?',(h['id'],)).fetchone()
        detail=('Waiting for prior delivery '+blocker['id']+' ('+blocker['status']+'). '+blocker['detail'][:300]+
                ' Exact-turn reconciliation is required; no new attempt.')
        at=self.clock()
        db.execute('UPDATE handoffs SET status=?,detail=?,updated=?,next_check=? WHERE id=?',('BUSY',detail,at,at+10,h['id']))
        if tuple(previous)!=('BUSY',detail):db.execute('INSERT INTO events(handoff_id,at,status,detail) VALUES(?,?,?,?)',(h['id'],at,'BUSY',detail))
    def step(self,h):
        m=self.message(h['id']);target=json.loads(h['target']);receipt=json.loads(h['receipt'])
        if hashlib.sha256(m['body'].encode()).hexdigest()!=h['body_sha']:
            self.update(h['id'],'FAILED','Original message changed; no delivery permitted.');return
        if h['status'] in ACTIVE:
            # Reconcile even after a permission revocation; never send again.
            obs=self.transport.observe(target,h['request_id'],receipt)
            if obs.get('status')=='FAILED' and self.turn_still_open(h,obs):
                # History can report a transient failure (e.g. a mid-turn error before
                # auto-compaction) while the turn keeps running. Only a closed turn fails.
                self.update(h['id'],'RUNNING','Turn still running after a recoverable error; not treated as failed.',{**receipt,**obs.get('receipt',{})})
            elif obs.get('status') in ('ACCEPTED','RUNNING','RETURNED','FAILED'):
                self.update(h['id'],obs['status'],obs.get('detail','Exact provider evidence observed.'),{**receipt,**obs.get('receipt',{})})
            elif h['status']=='SENDING' and self.clock()-h['updated']>60:
                self.update(h['id'],'UNCERTAIN','No delivery receipt after send; reconciliation only, no automatic retry.',{**receipt,**obs.get('receipt',{})})
            elif h['status']=='UNCERTAIN' and self.proven_absent(obs):
                # Complete marker scans found no turn: the provider never received it.
                # Release the recipient queue only; never resend (the inbox message is unchanged).
                first=receipt.get('firstAbsentAt') or self.clock();absent=int(receipt.get('absentHistoryScans') or 0)+1
                merged={**receipt,**obs.get('receipt',{}),'absentHistoryScans':absent,'firstAbsentAt':first}
                if absent>=ABSENT_SCANS and self.clock()-first>=ABSENT_SECONDS:
                    self.update(h['id'],'CANCELLED','Released: never delivered. '+str(absent)+' complete scans of the recipient history over '+
                                str(int((self.clock()-first)//60))+' min found no turn for this request. Nothing was resent; the original inbox message is unchanged.',merged)
                else:self.update(h['id'],h['status'],obs.get('detail',h['detail'])+' Absent scans: '+str(absent)+'/'+str(ABSENT_SCANS)+'.',merged)
            elif obs.get('receipt'):
                self.update(h['id'],h['status'],obs.get('detail',h['detail']),{**receipt,**obs['receipt']})
            return
        if m.get('read_at') and not (receipt.get('resends') and self.work_open(h['id'])):
            # A resend exists because accepted work is still unfinished; reading it earlier does not finish it.
            self.update(h['id'],'ACKNOWLEDGED','Recipient acknowledged the original inbox message; no extra turn needed.');return
        state=self.flow.read()
        from workflow_work import alert_permission
        alert=alert_permission(self.flow,m,state)
        if alert and alert['resolved']:
            self.update(h['id'],'CANCELLED','Original work was closed before the overdue alert could be delivered.');return
        if not state.get('enabled') or not (alert['allowed'] if alert is not None else allowed(state,h['sender'],h['recipient'])):
            self.update(h['id'],'HELD','Saved directed connection does not allow this handoff.');return
        current=next((s for s in self.flow.identity_catalog()['sessions'] if s['agent_id']==h['recipient']),None)
        if not current or current.get('endpoint')!=target.get('endpoint') or current.get('provider')!=target.get('provider'):
            self.update(h['id'],'UNAVAILABLE','Original exact recipient no longer resolves; no substitute agent was selected.');return
        if current.get('managed'):
            self.update(h['id'],'MANAGED_POLICY','Managed worker needs its existing approved TaskFlow assignment/runtime; a handoff cannot renew exhausted runs.');return
        outage=self.provider_outage()
        if outage:
            # Keep status, attempts, request identity and receipt; only explain the wait.
            self.update(h['id'],h['status'],outage,delay=60);return
        activity=self.flow.activity_snapshot([h['recipient']])['activities'].get(h['recipient']) or {}
        if activity.get('turnStatus')=='open' or activity.get('active'):
            self.update(h['id'],'BUSY','Recipient is working; wait for its existing turn to finish.');return
        if activity.get('turnStatus') not in ('finished','stopped','failed'):
            self.update(h['id'],'UNAVAILABLE','Recipient activity cannot be verified. No blind wake or replacement session.');return
        retry_resume=receipt.get('rejectedBeforeAcceptance') is True and h['attempts']==1
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,attempts FROM handoffs WHERE id=?',(h['id'],)).fetchone()
            if row['status'] not in PENDING or (row['attempts'] and not retry_resume):return
            blocker=self.active_blocker(db,h)
            if blocker:
                self.block_pending(db,h,blocker);return
        # A retry normally resumes the unloaded task directly. If that resume already
        # met an active writer, the desktop app has since loaded the task: deliver
        # through the desktop owner instead of retrying a resume that must fail.
        force=retry_resume and not receipt.get('activeWriterSeen')
        preparation=self.transport.prepare(current,force_resume=True) if force else self.transport.prepare(current)
        with preparation as prepared:
            # Registration can fail without a provider write. Do it before the
            # durable send reservation so failures remain safely recoverable.
            if hasattr(self.transport,'before_send'):self.transport.before_send(prepared,h['request_id'])
            # The provider preflight can take time: recheck route and activity at the send boundary.
            current_state=self.flow.read()
            alert=alert_permission(self.flow,m,current_state)
            if alert and alert['resolved']:
                self.update(h['id'],'CANCELLED','Original work closed during alert preflight.');return
            if not self.policy().get('enabled') or not current_state.get('enabled') or not (alert['allowed'] if alert is not None else allowed(current_state,h['sender'],h['recipient'])):
                self.update(h['id'],'HELD','Delivery was paused or its connection was revoked during preflight.');return
            a=self.flow.activity_snapshot([h['recipient']])['activities'].get(h['recipient']) or {}
            if a.get('turnStatus') not in ('finished','stopped','failed'):
                self.update(h['id'],'BUSY','Recipient became busy during preflight.');return
            prepared_receipt=self.transport.start_receipt(prepared,h['request_id']) if hasattr(self.transport,'start_receipt') else {}
            prepared_receipt={**receipt,**prepared_receipt,'rejectedBeforeAcceptance':False}
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT status,attempts FROM handoffs WHERE id=?',(h['id'],)).fetchone()
                if row['status'] not in PENDING or (row['attempts'] and not retry_resume):return
                blocker=self.active_blocker(db,h)
                if blocker:
                    self.block_pending(db,h,blocker);return
                db.execute("UPDATE handoffs SET status='SENDING',attempts=attempts+1,updated=?,receipt=? WHERE id=?",(self.clock(),json.dumps(prepared_receipt),h['id']))
                db.execute('INSERT INTO events(handoff_id,at,status,detail) VALUES(?,?,?,?)',(h['id'],self.clock(),'SENDING','One provider write reserved atomically.'))
            receipt=prepared_receipt
            try:
                result=self.transport.start(prepared,self.envelope(h,m,current),h['request_id'])
                if result.get('turnId') and result.get('clientUserMessageId')==h['request_id']:
                    self.update(h['id'],'ACCEPTED','Provider accepted one turn for the exact recipient.',result)
                else:self.update(h['id'],'UNCERTAIN','Provider send returned no exact turn receipt; reconciliation only.',result)
            except Exception as e:
                from workflow_handoff_transport import StartNotAccepted
                if isinstance(e,StartNotAccepted) and not force:
                    self.update(h['id'],'OWNER_REJECTED','Router confirmed no owner accepted the request; revalidate exclusive ownership before the one resume attempt.',
                        {**receipt,'rejectedBeforeAcceptance':True,'desktopRejection':str(e)})
                else:self.update(h['id'],'UNCERTAIN','Provider delivery result uncertain: '+str(e)[:400]+'. No automatic resend.',receipt)
    def tick(self):
        if not self.policy().get('enabled'):return
        lock=self.root/'runtime/workflow-handoffs.lock'
        with lock.open('a') as f:
            try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            started=self.monotonic();checked=0
            try:previous=json.loads(self.health_path.read_text())
            except (OSError,ValueError):previous={}
            # Progress never refreshes the completion heartbeat. A slow/stuck
            # operation still fails the unchanged45-second freshness boundary.
            health={'at':previous.get('at',0) if previous.get('pid')==os.getpid() else 0,
                    'ok':previous.get('ok',False) if previous.get('pid')==os.getpid() else False,
                    'pid':os.getpid(),'checked':0,'scanStartedAt':self.clock()}
            def progress(phase,mid=None):
                health.update(phase=phase,currentMessageId=mid,checked=checked,
                              elapsedSeconds=max(0,self.monotonic()-started))
                atomic_json(self.health_path,health)
            progress('deadline-check')
            from workflow_work import alert_overdue
            alert_overdue(self.flow,at=self.clock())
            progress('enrollment')
            enrollment=self.scan()
            try:self.review_failures()
            except Exception:pass  # never let failure review stop delivery
            cursor=self.scan_cursor('due');at=self.clock()
            with self.connect() as db:
                query='SELECT rowid AS scan_sequence,* FROM handoffs WHERE status NOT IN (?,?,?,?,?) AND next_check<=?'
                after=' AND (created>? OR (created=? AND rowid>?))'
                args=(*TERMINAL,at)
                rows=[dict(r) for r in db.execute(query+(after if cursor else '')+' ORDER BY created,rowid LIMIT ?',
                    args+((cursor[0],cursor[0],cursor[1]) if cursor else ())+(SCAN_ROWS,))]
                if not rows and cursor:
                    rows=[dict(r) for r in db.execute(query+' ORDER BY created,rowid LIMIT ?',args+(SCAN_ROWS,))]
            work_started=self.monotonic()
            for h in rows:
                # Cooperative budget: complete the current existing operation;
                # never cancel it, launch a helper or begin an extra provider turn.
                if checked and self.monotonic()-work_started>=SCAN_SECONDS:break
                progress('delivery-check',h['id'])
                try:self.step(h)
                except Exception as e:
                    current=self.get(h['id'])
                    rejected=json.loads(current['receipt']).get('rejectedBeforeAcceptance') is True and current['attempts']==1
                    status='OWNER_REJECTED' if rejected else ('UNCERTAIN' if current['attempts'] else 'UNAVAILABLE')
                    marked=None
                    if rejected and 'already has an active writer' in str(e):
                        marked={**json.loads(current['receipt']),'activeWriterSeen':True}
                    self.update(h['id'],status,str(e)[:600],marked,delay=30)
                self.advance_scan('due',h['created'],h['scan_sequence']);checked+=1
            if checked==len(rows) and len(rows)<SCAN_ROWS:self.reset_scan('due')
            with self.connect() as db:
                remaining=db.execute('SELECT COUNT(*) FROM handoffs WHERE status NOT IN (?,?,?,?,?) AND next_check<=?',(*TERMINAL,self.clock())).fetchone()[0]
            health.update(at=self.clock(),ok=not (enrollment or {}).get('errors'),phase='batch-complete',
                          currentMessageId=None,checked=checked,remainingDue=remaining,
                          enrollment=enrollment,elapsedSeconds=max(0,self.monotonic()-started),
                          scope='Completed bounded batch; queue completion and delivery acceptance remain separate.')
            atomic_json(self.health_path,health)
