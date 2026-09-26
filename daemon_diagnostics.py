"""Content-free daemon exception evidence. No SQL, parameters or row contents."""
import os
from pathlib import Path
import sqlite3
import threading
import time
import traceback

SAFE_ERRORS={'disk I/O error','database is locked','database is busy','database table is locked','unable to open database file','attempt to write a readonly database','database disk image is malformed'}

def describe(exc,duty):
    frames=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
            for f in traceback.extract_tb(exc.__traceback__)[-12:]]
    code=getattr(exc,'sqlite_errorcode',None)
    result={'schemaVersion':'ke.daemon-failure.v1','at':time.time(),'duty':duty,
            'pid':os.getpid(),'threadId':threading.get_ident(),'threadName':threading.current_thread().name,
            'exceptionType':type(exc).__name__,'message':str(exc) if str(exc) in SAFE_ERRORS else '[content omitted]',
            'sqliteVersion':sqlite3.sqlite_version,'sqliteErrorCode':code,
            'sqliteErrorName':getattr(exc,'sqlite_errorname',None),
            'codeAvailability':'native-exception' if code is not None else 'not exposed by this Python SQLite exception; not inferred',
            'operation':getattr(exc,'productionOperation',None),'frames':frames}
    connection=getattr(exc,'productionConnection',None)
    if isinstance(connection,dict):result['productionConnection']=connection
    # Retain metadata only; never open/hash the live DB or journal as raw files.
    path=getattr(exc,'productionDatabase',None)
    if path:
        path=Path(path);files=[]
        for suffix in ('','-journal','-wal','-shm'):
            target=Path(str(path)+suffix)
            try:
                s=target.stat();files.append({'name':target.name,'device':s.st_dev,'inode':s.st_ino,'bytes':s.st_size,'mtimeNs':s.st_mtime_ns})
            except OSError as e:files.append({'name':target.name,'statErrno':e.errno})
        result['databaseFiles']=files
    return result
