#!/usr/bin/env python3
"""Consistent compressed backups and an isolated restore verification."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import board_core as board


def backup_current():
    """Daily small recovery snapshot; legacy event archive stays in the full backup.

    This deliberately excludes the 26M-row diagnostic event archive. All current
    ownership, task contracts/history/proofs, pending deliveries, message index,
    read cursors and monitor registrations are copied consistently.
    """
    tables=['teams','agents','incidents','monitors','coordination_meta','tasks',
            'task_events','task_proofs','room_outbox','handoffs','route_observations',
            'room_messages','reader_cursors','incident_links','monitor_observations']
    destination=board.ROOT/'backups';destination.mkdir(exist_ok=True,mode=0o700)
    name=time.strftime('current-%Y%m%dT%H%M%SZ.json.gz',time.gmtime())
    target=destination/name
    with board.connect() as c:
        c.execute('BEGIN')
        data={'contract':'ke.coordination.recovery.v2','created_at':board.utc_now(),
              'coverage':'current coordination state; legacy events remain in full archive',
              'legacy_event_high_watermark':c.execute('SELECT coalesce(max(sequence),0) FROM events').fetchone()[0],
              'tables':{t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}}
    import current_context
    data['currentContextRegistry'] = current_context.snapshot_registry(board.ROOT)
    import workflow_layout
    data['workflowStores'] = workflow_layout.snapshot_stores(board.ROOT)
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    temp=target.with_suffix('.tmp')
    with gzip.open(temp,'wb',compresslevel=1) as f:f.write(raw)
    with gzip.open(temp,'rb') as f:
        if hashlib.sha256(f.read()).digest()!=hashlib.sha256(raw).digest():raise ValueError('snapshot verification failed')
    os.chmod(temp,0o600);os.replace(temp,target)
    with board.connect() as c:
        c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('last_current_backup',?)",(str(time.time()),))
        c.execute("INSERT OR REPLACE INTO coordination_meta VALUES('current_backup_path',?)",(str(target),))
    return {'ok':True,'path':str(target),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}


def verify_current(path):
    import coordination
    with gzip.open(path,'rb') as f:data=json.load(f)
    with tempfile.TemporaryDirectory() as tmp:
        c=sqlite3.connect(Path(tmp)/'restored.sqlite3')
        c.executescript(board.SCHEMA+coordination.SCHEMA)
        coordination.migrate_legacy(c)
        for table,rows in data['tables'].items():
            if table not in ['teams','agents','incidents','monitors','coordination_meta','tasks','task_events','task_proofs','room_outbox','handoffs','route_observations','room_messages','reader_cursors','incident_links','monitor_observations']:
                raise ValueError('unexpected recovery table')
            c.execute('DELETE FROM '+table) # isolated disposable verification DB only
            if rows:
                columns=[r[1] for r in c.execute('PRAGMA table_info('+table+')')]
                provided=list(rows[0])
                if not set(provided).issubset(columns):raise ValueError('schema mismatch')
                c.executemany('INSERT INTO '+table+' ('+','.join(provided)+') VALUES('+','.join('?' for _ in provided)+')',[[r[k] for k in provided] for r in rows])
        c.commit();check=c.execute('PRAGMA quick_check').fetchone()[0];c.close()
        context_recovery = {'coverage':'not present in this historical backup'}
        if 'currentContextRegistry' in data:
            import current_context
            context_recovery = current_context.restore_registry_snapshot(Path(tmp)/'context-restore', data['currentContextRegistry'])
        workflow_recovery = {'coverage':'not present in this historical backup'}
        if 'workflowStores' in data:
            import workflow_layout
            workflow_recovery = workflow_layout.restore_stores(Path(tmp)/'workflow-restore',data['workflowStores'])
    if check!='ok':raise ValueError(check)
    return {'ok':True,'quick_check':check,'live_database_touched':False,'tables':{t:len(r) for t,r in data['tables'].items()},'currentContextRegistry':context_recovery,'workflowStores':workflow_recovery}


def backup(destination):
    destination=Path(destination).resolve()
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
    output=destination/('board-'+stamp+'.sqlite3.gz')
    if output.exists(): raise ValueError('backup already exists')
    # The SQLite backup API creates a transaction-consistent snapshot including WAL.
    with tempfile.TemporaryDirectory(dir=destination) as tmp:
        snapshot=Path(tmp)/'snapshot.sqlite3'
        src=sqlite3.connect('file:'+str(board.DB_PATH)+'?mode=ro',uri=True)
        dst=sqlite3.connect(snapshot)
        src.backup(dst,pages=4096);src.close()
        check=dst.execute('PRAGMA quick_check').fetchone()[0]
        dst.close()
        if check!='ok': raise ValueError('backup integrity failed: '+check)
        raw_hash=hashlib.sha256()
        with snapshot.open('rb') as a,gzip.open(str(output)+'.tmp','wb',compresslevel=1) as z:
            while True:
                chunk=a.read(1024*1024)
                if not chunk:break
                raw_hash.update(chunk);z.write(chunk)
        os.replace(str(output)+'.tmp',output);os.chmod(output,0o600)
    receipt={'path':str(output),'uncompressed_sha256':raw_hash.hexdigest(),'quick_check':'ok',
             'created_at':board.utc_now(),'restore_command':'maintenance.py verify-backup '+str(output)}
    output.with_suffix(output.suffix+'.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt


def verify_backup(path):
    path=Path(path);receipt=json.loads(path.with_suffix(path.suffix+'.json').read_text())
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        restored=Path(tmp)/'restored.sqlite3';h=hashlib.sha256()
        with gzip.open(path,'rb') as z,restored.open('wb') as out:
            while True:
                chunk=z.read(1024*1024)
                if not chunk:break
                h.update(chunk);out.write(chunk)
        if h.hexdigest()!=receipt['uncompressed_sha256']:raise ValueError('backup digest mismatch')
        c=sqlite3.connect('file:'+str(restored)+'?mode=ro&immutable=1',uri=True)
        check=c.execute('PRAGMA quick_check').fetchone()[0];c.close()
        if check!='ok':raise ValueError(check)
    return {'ok':True,'sha256':h.hexdigest(),'quick_check':check,'live_database_touched':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['backup','verify-backup','backup-current','verify-current']);p.add_argument('path',nargs='?');a=p.parse_args()
    actions={'backup':lambda:backup(a.path),'verify-backup':lambda:verify_backup(a.path),
             'backup-current':backup_current,'verify-current':lambda:verify_current(a.path)}
    print(json.dumps(actions[a.command]()))
