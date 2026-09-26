"""Evidence-bound current context for local briefings, never execution authority.

Only explicitly registered manifests and their exact documentary references are
read. No repository discovery, input execution or historical task mutation occurs.
"""
from __future__ import annotations

import fcntl
import base64
import hashlib
import json
import os
import re
import stat
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 'ke.current-context.v1'
REGISTRY_SCHEMA = 'ke.current-context-registry.v1'
MAX_MANIFEST = 131072
MAX_DOCUMENT = 1048576
MAX_DOCUMENT_TOTAL = 8388608
MAX_REGISTRY = 524288
MAX_OUTPUT = 65536
MAX_COMPONENTS = 32
MAX_EVIDENCE = 128
MAX_CONTEXTS = 8
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
EXTENSIONS = {'.json', '.md', '.txt', '.py'}
STATES = {
    'implementation': {'implemented', 'partial', 'not-implemented', 'unknown'},
    'testing': {'passed', 'partial', 'not-tested', 'unknown'},
    'integration': {'integrated', 'partial', 'not-integrated', 'not-applicable', 'unknown'},
    'qualification': {'qualified', 'unproven', 'not-applicable', 'unknown'},
}
POSITIVE = {'implemented', 'passed', 'integrated', 'qualified'}
BOUNDARY = ('Context only: matching hashes and assertions verify the declared evidence, '
            'not scientific qualification, profit, live readiness or execution authority. '
            'Later unregistered evidence may exist; no exhaustive search is implied.')


class ContextError(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise ContextError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def text(value, name, maximum=600):
    need(isinstance(value, str) and 0 < len(value) <= maximum, 'invalid '+name)
    return value


def identifier(value, name):
    need(isinstance(value, str) and ID.fullmatch(value), 'invalid '+name)
    return value


def timestamp(value):
    text(value, 'asOf', 40)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        need(result.tzinfo is not None, 'asOf must include timezone')
        need((result-datetime.now(timezone.utc)).total_seconds() <= 300, 'asOf is in the future')
        return result
    except (TypeError, ValueError) as exc:
        raise ContextError('invalid asOf: '+str(exc)) from exc


def read_bytes(path, limit):
    """One bounded regular-file read with stable identity; never execute content."""
    p = Path(path)
    need(p.is_absolute(), 'absolute path required')
    fd = os.open(str(p), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode), 'regular file required')
        need(before.st_size <= limit, 'file exceeds byte bound')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit+1)
        after = os.fstat(fd)
        key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        need(key(before) == key(after) and len(raw) == after.st_size and len(raw) <= limit,
             'file changed during bounded read')
        return raw
    finally:
        os.close(fd)


def parsed(raw):
    try:
        def pairs(items):
            out = {}
            for k, v in items:
                need(k not in out, 'duplicate JSON key')
                out[k] = v
            return out
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ContextError('nonfinite JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContextError('invalid bounded JSON') from exc


def pointer(value, path):
    need(isinstance(path, str) and (path == '' or path.startswith('/')), 'invalid JSON pointer')
    for part in path.split('/')[1:] if path else []:
        need(not re.search(r'~(?![01])', part), 'invalid JSON pointer escape')
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            need(bool(re.fullmatch(r'0|[1-9][0-9]*', part)) and int(part) < len(value), 'JSON pointer missing')
            value = value[int(part)]
        else:
            need(isinstance(value, dict) and part in value, 'JSON pointer missing')
            value = value[part]
    return value


def validate_manifest(data, owner=None):
    need(isinstance(data, dict) and data.get('schemaVersion') == SCHEMA, 'manifest schema mismatch')
    identifier(data.get('contextId'), 'contextId')
    text(data.get('owner'), 'owner', 150)
    need(owner is None or data['owner'] == owner, 'manifest owner mismatch')
    identifier(data.get('project'), 'project')
    text(data.get('objective'), 'objective', 1500)
    timestamp(data.get('asOf'))
    if 'summary' in data: text(data['summary'], 'summary', 1500)
    boundaries = data.get('boundaries', [])
    need(isinstance(boundaries, list) and len(boundaries) <= 16, 'invalid boundaries')
    for row in boundaries: text(row, 'boundary', 600)
    components = data.get('components')
    need(isinstance(components, list) and 0 < len(components) <= MAX_COMPONENTS, 'component count bound')
    ids, evidence_count = set(), 0
    for c in components:
        need(isinstance(c, dict), 'invalid component')
        cid = identifier(c.get('id'), 'component id')
        need(cid not in ids, 'duplicate component id'); ids.add(cid)
        text(c.get('title'), 'title', 200); text(c.get('purpose'), 'purpose', 1000)
        evidence = c.get('evidence')
        need(isinstance(evidence, list), 'component evidence required')
        evidence_count += len(evidence)
        need(evidence_count <= MAX_EVIDENCE, 'evidence count bound')
        refs = {}
        for e in evidence:
            need(isinstance(e, dict), 'invalid evidence')
            eid = identifier(e.get('id'), 'evidence id')
            need(eid not in refs, 'duplicate evidence id'); refs[eid] = e
            path = Path(text(e.get('path'), 'evidence path', 1500))
            need(path.is_absolute() and path.suffix.lower() in EXTENSIONS, 'documentary evidence path required')
            need(isinstance(e.get('sha256'), str) and SHA.fullmatch(e['sha256']), 'evidence SHA required')
            assertions = e.get('assertions', [])
            need(isinstance(assertions, list) and len(assertions) <= 32, 'assertion count bound')
            need(not assertions or path.suffix.lower() == '.json', 'assertions require JSON evidence')
            for a in assertions:
                need(isinstance(a, dict) and set(a) == {'pointer','equals'}, 'invalid assertion')
                need(isinstance(a['pointer'], str) and len(a['pointer']) <= 500 and
                     (a['pointer'] == '' or a['pointer'].startswith('/')), 'invalid assertion pointer')
                need(len(encoded(a['equals'])) <= 16384, 'assertion value bound')
        states = c.get('states')
        need(isinstance(states, dict) and set(states) == set(STATES), 'all four proof dimensions required')
        for dimension, state in states.items():
            need(isinstance(state, dict) and state.get('status') in STATES[dimension], 'invalid '+dimension+' state')
            es = state.get('evidence')
            need(isinstance(es, list) and len(es) <= 16 and
                 all(isinstance(x, str) and x in refs for x in es) and len(es) == len(set(es)), 'unknown state evidence')
            if state['status'] in POSITIVE:
                need(bool(es), 'positive state requires evidence')
                if dimension != 'implementation':
                    need(any(refs[x].get('assertions') for x in es), 'positive proof state requires an explicit assertion')
            if 'detail' in state: text(state['detail'], 'state detail', 1000)
        acceptance = c.get('latestAcceptance')
        need(acceptance is None or acceptance in refs, 'unknown latest acceptance')
        if acceptance is not None:
            need(bool(refs[acceptance].get('assertions')), 'acceptance requires explicit assertion')
        history = c.get('historical', [])
        need(isinstance(history, list) and len(history) <= 16, 'historical count bound')
        for h in history:
            need(isinstance(h, dict) and h.get('evidence') in refs and h.get('supersededBy') in refs,
                 'unknown historical evidence')
            need(h['evidence'] != h['supersededBy'] and bool(refs[h['supersededBy']].get('assertions')),
                 'superseding evidence requires independent assertion')
            text(h.get('status'), 'historical status', 150)
        gap = c.get('gap')
        need(gap is None or isinstance(gap, dict), 'invalid gap')
        if gap is not None:
            for k in ['description','owner','nextAction']: text(gap.get(k), 'gap '+k, 1500)
        task_ids = c.get('historicalTaskIds', [])
        need(isinstance(task_ids, list) and len(task_ids) <= 16, 'historical task count bound')
        for task_id in task_ids: identifier(task_id, 'historical task id')
    return data


def verify_manifest(path, expected_sha256, owner=None):
    need(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256), 'manifest SHA required')
    raw = read_bytes(path, MAX_MANIFEST)
    need(hashlib.sha256(raw).hexdigest() == expected_sha256, 'manifest hash changed')
    data = validate_manifest(parsed(raw), owner)
    cache, total, components = {}, 0, []
    for c in data['components']:
        checks = {}
        for e in c['evidence']:
            key = (e['path'], e['sha256'])
            if key not in cache:
                try:
                    raw_e = read_bytes(e['path'], min(MAX_DOCUMENT, MAX_DOCUMENT_TOTAL-total))
                    total += len(raw_e)
                    need(hashlib.sha256(raw_e).hexdigest() == e['sha256'], 'evidence hash changed')
                    cache[key] = (raw_e, None)
                except (OSError, ContextError) as exc:
                    cache[key] = (None, str(exc))
            raw_e, error = cache[key]
            if error is None and e.get('assertions'):
                try:
                    value = parsed(raw_e)
                    for a in e['assertions']:
                        need(encoded(pointer(value, a['pointer'])) == encoded(a['equals']), 'evidence assertion mismatch')
                except ContextError as exc: error = str(exc)
            checks[e['id']] = {'id':e['id'], 'path':e['path'], 'sha256':e['sha256'],
                               'status':'INVALID' if error else 'VERIFIED', 'error':error}
        errors = [r for r in checks.values() if r['status'] != 'VERIFIED']
        states = {}
        for dimension, state in c['states'].items():
            valid = all(checks[x]['status'] == 'VERIFIED' for x in state['evidence'])
            states[dimension] = {**state, 'status':state['status'] if valid else 'unknown',
                                 'declaredStatus':state['status'], 'evidenceVerified':valid}
        history = []
        for h in c.get('historical', []):
            valid = all(checks[x]['status'] == 'VERIFIED' for x in [h['evidence'], h['supersededBy']])
            history.append({**h, 'display':'HISTORICAL_SUPERSEDED' if valid else 'UNRESOLVED',
                            'historicalRecordChanged':False})
        accepted = c.get('latestAcceptance')
        components.append({'id':c['id'], 'title':c['title'], 'purpose':c['purpose'],
            'status':'EVIDENCE_INVALID' if errors else 'EVIDENCE_VERIFIED', 'states':states,
            'evidence':list(checks.values()), 'latestAcceptance':accepted,
            'latestAcceptanceVerified':accepted is not None and checks[accepted]['status'] == 'VERIFIED',
            'historical':history, 'gap':c.get('gap'),
            'historicalTaskIds':c.get('historicalTaskIds', []),
            'nextAction':('Repair or explicitly rebind changed evidence before relying on affected states.' if errors
                          else (c.get('gap') or {}).get('nextAction','Retain this completed component; reopen only for a concrete defect or changed requirement.'))})
    invalid = sum(c['status'] != 'EVIDENCE_VERIFIED' for c in components)
    return {'schemaVersion':SCHEMA, 'contextId':data['contextId'], 'owner':data['owner'],
        'project':data['project'], 'objective':data['objective'], 'summary':data.get('summary'),
        'boundaries':data.get('boundaries', []), 'manifestPath':str(path), 'manifestSHA256':expected_sha256,
        'status':'PARTIAL_EVIDENCE_INVALID' if invalid else 'EVIDENCE_VERIFIED',
        'freshness':{'verifiedAt':utc_now(), 'declaredAsOf':data['asOf'],
                     'declarationAgeSeconds':max(0, int((datetime.now(timezone.utc)-timestamp(data['asOf'])).total_seconds())),
                     'exhaustiveCurrentState':False},
        'coverage':{'declaredComponents':len(components), 'displayedComponents':len(components),
                    'invalidComponents':invalid, 'documentBytesRead':total},
        'components':components, 'authority':False, 'proofBoundary':BOUNDARY}


def registry_path(root):
    return Path(root)/'runtime'/'current-context'/'registry.json'


def read_registry(root):
    p = registry_path(root)
    if not p.exists(): return {'schemaVersion':REGISTRY_SCHEMA, 'contexts':{}}
    data = parsed(read_bytes(p, MAX_REGISTRY))
    need(isinstance(data, dict) and data.get('schemaVersion') == REGISTRY_SCHEMA and
         isinstance(data.get('contexts'), dict) and len(data['contexts']) <= 64, 'registry invalid')
    return data


def register(root, manifest_path, expected_sha256, owner, owner_endpoint, audience=(), expected_revision=0):
    """Explicit registration is local context ownership, not issuer authentication."""
    projection = verify_manifest(manifest_path, expected_sha256, owner)
    need(projection['status'] == 'EVIDENCE_VERIFIED', 'cannot register invalid evidence')
    audience = sorted(set([owner_endpoint, *audience]))
    need(0 < len(audience) <= 128, 'audience count bound')
    for endpoint in audience: text(endpoint, 'audience endpoint', 150)
    path = registry_path(root); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(path.parent/'registry.lock'), os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = read_registry(root); old = data['contexts'].get(projection['contextId'])
        need(expected_revision == (old['revision'] if old else 0), 'context revision conflict')
        need(old is None or old['owner'] == owner, 'registered context owner mismatch')
        need(old is not None or len(data['contexts']) < 64, 'registry count bound')
        record = {'contextId':projection['contextId'], 'owner':owner, 'ownerEndpoint':owner_endpoint,
                  'project':projection['project'], 'manifestPath':str(manifest_path), 'manifestSHA256':expected_sha256,
                  'audience':audience, 'revision':expected_revision+1, 'registeredAt':utc_now(),
                  'components':[{'id':c['id'],'title':c['title'],
                                  'historicalTaskIds':c['historicalTaskIds']} for c in projection['components']]}
        data['contexts'][record['contextId']] = record; raw = encoded(data)+b'\n'
        need(len(raw) <= MAX_REGISTRY, 'registry byte bound')
        history = path.parent/'history'; history.mkdir(exist_ok=True, mode=0o700)
        history_path = history/(record['contextId']+'.'+str(record['revision'])+'.json')
        if history_path.exists():
            # Retry after an interrupted replacement reuses its preserved registration.
            retained = parsed(read_bytes(history_path, MAX_REGISTRY))
            need({k:v for k,v in retained.items() if k != 'registeredAt'} ==
                 {k:v for k,v in record.items() if k != 'registeredAt'}, 'registration history conflict')
            record = retained; data['contexts'][record['contextId']] = record
            raw = encoded(data)+b'\n'
        else:
            with history_path.open('xb') as h: h.write(encoded(record)+b'\n')
        fd, temporary = tempfile.mkstemp(prefix='.registry-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return {**record, 'status':'REGISTERED', 'authority':False}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)


def permitted_audience(root, record, endpoint):
    """Own context stays readable; cross-session context follows the operator's graph."""
    if endpoint == record['ownerEndpoint']:
        return True
    try:
        from workflow import read_state, allowed
        state = read_state(root)
        if not state['enabled']:
            return True
        database = Path(root)/'board.sqlite3'
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=3) as db:
            recipients = [r[0] for r in db.execute('SELECT agent_id FROM agents WHERE endpoint=?', (endpoint,))]
        return any(allowed(state, record['owner'], recipient) for recipient in recipients)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
        return False


def project_for_endpoint(root, endpoint):
    result = {'schemaVersion':'ke.current-context-brief.v1', 'status':'UNREGISTERED',
              'contexts':[], 'coverage':{'registered':0,'displayed':0,'omittedContextIds':[]},
              'authority':False, 'proofBoundary':BOUNDARY}
    try:
        registry = read_registry(root)
        records = sorted([r for r in registry['contexts'].values()
                          if endpoint in r['audience'] and permitted_audience(root, r, endpoint)],
                         key=lambda r:r['contextId'])
        result['coverage']['registered'] = len(records)
        result['coverage']['omittedContextIds'] = [r['contextId'] for r in records[MAX_CONTEXTS:]]
        for record in records[:MAX_CONTEXTS]:
            try:
                p = verify_manifest(record['manifestPath'], record['manifestSHA256'], record['owner'])
                need(p['contextId'] == record['contextId'], 'registered context identity changed')
                p['registrationRevision'] = record['revision']
            except (OSError, ContextError, KeyError, TypeError) as exc:
                p = {'contextId':record['contextId'], 'status':'CONTEXT_UNAVAILABLE',
                     'manifestPath':record['manifestPath'], 'manifestSHA256':record['manifestSHA256'],
                     'registeredComponents':record['components'], 'components':[], 'error':str(exc),
                     'nextAction':'Restore exact registered evidence or have its owner explicitly register a verified successor; no completed-state inference is available.',
                     'authority':False}
            if len(encoded(p)) > MAX_OUTPUT//MAX_CONTEXTS:
                # A compact complete ID/state view remains visible; detailed evidence stays addressable.
                p['detailsOmitted'] = True
                for c in p.get('components', []):
                    c.pop('purpose', None)
                    c['evidence'] = [{'id':e['id'],'status':e['status'],'error':e['error']} for e in c['evidence']]
                p['detailCommand'] = 'boardctl.py context-show --context-id '+record['contextId']
            result['contexts'].append(p)
        result['coverage']['displayed'] = len(result['contexts'])
        if records:
            result['status'] = ('PARTIAL' if result['coverage']['omittedContextIds'] or
                                any(c['status'] != 'EVIDENCE_VERIFIED' for c in result['contexts']) else 'EVIDENCE_VERIFIED')
        if len(encoded(result)) > MAX_OUTPUT:
            result['status'] = 'OUTPUT_BOUND_EXCEEDED'
            result['contexts'] = [{'contextId':r['contextId'], 'status':'DETAIL_REQUIRED',
                                   'registeredComponentIds':[c['id'] for c in r['components']],
                                   'detailCommand':'boardctl.py context-show --context-id '+r['contextId']} for r in records[:MAX_CONTEXTS]]
        result['nextAction'] = ('Read currentContext before planning; use context-show for omitted details. '
                                'Do not treat historical pending fields as current when verified superseding evidence is displayed.'
                                if records else 'No current context is registered for this endpoint. Reconcile relevant evidence before inferring what is missing.')
    except (OSError, ContextError, KeyError, TypeError) as exc:
        result.update(status='REGISTRY_UNAVAILABLE', error=str(exc),
                      nextAction='Repair the registered context index; do not infer completed or missing implementation from this incomplete briefing.')
    return result


def annotate_tasks(tasks, projection):
    """Overlay the displayed next action only; database rows and artifacts stay intact."""
    matches = {}
    for context in projection.get('contexts', []):
        for c in context.get('components') or context.get('registeredComponents', []):
            for task_id in c.get('historicalTaskIds', []):
                matches.setdefault(task_id, []).append({
                    'contextId':context['contextId'], 'componentId':c['id'],
                    'status':c.get('status', context['status']),
                    'nextAction':c.get('nextAction', context.get('nextAction', 'Read the current component evidence.'))})
    result = []
    for task in tasks:
        row = dict(task); linked = matches.get(task['task_id'], [])
        if linked:
            row['recordMeaning'] = 'Historical stage record; current component state is supplied separately.'
            row['historical_next_action'] = row.get('next_action')
            row['currentContext'] = linked
            row['next_action_source'] = 'currentContext display overlay; stored task unchanged'
            if len(linked) == 1:
                row['next_action'] = linked[0]['nextAction']
            else:
                row['next_action'] = 'Multiple current component references exist; reconcile them before choosing a next action.'
        result.append(row)
    return result


def show(root, context_id):
    identifier(context_id, 'contextId')
    record = read_registry(root)['contexts'].get(context_id)
    need(record is not None, 'context not registered')
    return verify_manifest(record['manifestPath'], record['manifestSHA256'], record['owner'])


RECOVERY_SCHEMA = 'ke.current-context-recovery.v1'
MAX_RECOVERY = 32 * 1024 * 1024


def snapshot_registry(root):
    """Preserve the exact index and revision history under the registration lock."""
    directory = registry_path(root).parent
    result = {'schemaVersion':RECOVERY_SCHEMA, 'files':[], 'documentaryEvidenceIncluded':False}
    if not directory.exists():
        return result
    fd = os.open(str(directory/'registry.lock'), os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        paths = ([directory/'registry.json'] if (directory/'registry.json').exists() else [])
        paths += sorted((directory/'history').glob('*.json'))
        need(len(paths) <= 4096, 'context recovery file bound exceeded')
        total = 0
        for p in paths:
            raw = read_bytes(p, min(MAX_REGISTRY, MAX_RECOVERY-total)); total += len(raw)
            result['files'].append({'path':str(p.relative_to(directory)),
                                    'sha256':hashlib.sha256(raw).hexdigest(),
                                    'base64':base64.b64encode(raw).decode('ascii')})
        return result
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def restore_registry_snapshot(root, snapshot):
    """Restore only into an absent directory, used by isolated backup verification."""
    need(snapshot.get('schemaVersion') == RECOVERY_SCHEMA, 'context recovery schema mismatch')
    files = snapshot.get('files'); need(isinstance(files, list) and len(files) <= 4096, 'context recovery file bound')
    directory = registry_path(root).parent
    need(not directory.exists(), 'refuse to overwrite an existing context registry')
    checked, total, names = [], 0, set()
    for row in files:
        name = row['path']
        need(name == 'registry.json' or bool(re.fullmatch(r'history/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.[1-9][0-9]*\.json', name)),
             'unexpected context recovery path')
        need(name not in names, 'duplicate context recovery path'); names.add(name)
        need(len(row['base64']) <= 4*((MAX_REGISTRY+2)//3), 'context recovery file byte bound')
        raw = base64.b64decode(row['base64'], validate=True); total += len(raw)
        need(len(raw) <= MAX_REGISTRY and total <= MAX_RECOVERY, 'context recovery byte bound')
        need(hashlib.sha256(raw).hexdigest() == row['sha256'], 'context recovery digest mismatch')
        checked.append((name, raw))
    for name, raw in checked:
        p = directory/name; p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with p.open('xb') as f: f.write(raw)
        os.chmod(p, 0o600)
    if checked:
        read_registry(root)
    return {'files':len(checked), 'bytes':total, 'documentaryEvidenceIncluded':False}


def add_cli(commands, board, emit):
    """Attach commands to the existing CLI without changing historical task APIs."""
    def register_command(args):
        board.init_db()
        agent = board.get_agent(args.owner)
        need(agent['provider'] in ('codex','claude'), 'a registered task owner is required')
        audience = args.endpoint or []
        with board.connect() as connection:
            for endpoint in audience:
                need(connection.execute('SELECT 1 FROM agents WHERE endpoint=? AND provider IN (?,?)',
                                        (endpoint,'codex','claude')).fetchone() is not None,
                     'audience endpoint is not registered')
        emit(register(board.ROOT,args.manifest,args.sha256,args.owner,agent['endpoint'],audience,args.version))

    registration = commands.add_parser('context-register', help='Register exact evidence-bound current context')
    registration.add_argument('--manifest', required=True)
    registration.add_argument('--sha256', required=True)
    registration.add_argument('--owner', required=True)
    registration.add_argument('--endpoint', action='append', default=[])
    registration.add_argument('--version', type=int, default=0, help='Expected existing registration revision, zero for new')
    registration.set_defaults(handler=register_command)
    view = commands.add_parser('context-show', help='Verify all details of one registered context')
    view.add_argument('--context-id', required=True)
    view.set_defaults(handler=lambda args:emit(show(board.ROOT,args.context_id)))
    validate = commands.add_parser('context-validate', help='Verify a proposed manifest without registering it')
    validate.add_argument('--manifest',required=True); validate.add_argument('--sha256',required=True)
    validate.add_argument('--owner',required=True)
    validate.set_defaults(handler=lambda args:emit(verify_manifest(args.manifest,args.sha256,args.owner)))
