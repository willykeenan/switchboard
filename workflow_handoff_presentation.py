"""Human presentation only. Never grants authority or changes work lifecycle.

Recorded prose is data. The renderer escapes Markdown/HTML, isolates originals as
JSON, and derives status/next actor from work records, not from message text.
"""
import json
import re
import unicodedata
from pathlib import Path

SCHEMA = 'ke.handoff-presentation.v1'
LIMITS = {'title': 60, 'result': 160, 'remaining': 120, 'nextStep': 100}
AVAILABILITY = {'unknown': 'Not established', 'not-installed': 'Not installed',
                'source-only': 'Source only', 'installed-unverified': 'Installed; verification unfinished',
                'live-verified': 'Live verification reported'}
DETAILS = '\n\n---\nTechnical details (agent-only)\n'


def validate(value):
    if not isinstance(value, dict) or set(value) != {'schema', *LIMITS, 'availability'} or value['schema'] != SCHEMA:
        raise ValueError('Use the exact ke.handoff-presentation.v1 fields')
    for field, cap in LIMITS.items():
        text = value[field]
        if not isinstance(text, str) or not text.strip() or len(text) > cap or any(unicodedata.category(c).startswith('C') for c in text):
            raise ValueError(field+' must be short, single-line plain text (maximum '+str(cap)+')')
        if re.search(r'\b[0-9a-f]{24,}\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|(?:' + '/Use' 'rs/' + r'|/Volumes/|https?://)', text, re.I):
            raise ValueError('Keep IDs, paths, hashes and links in technical evidence, not '+field)
    if value['availability'] not in AVAILABILITY:
        raise ValueError('Unknown reported availability')
    return dict(value)


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError('Use a bounded regular presentation file')
    return validate(json.loads(path.read_text()))


def plain(value, cap=180):
    value = ''.join(c for c in str(value or '') if not unicodedata.category(c).startswith('C'))
    value = ' '.join(value.split())
    if len(value) > cap: value = value[:cap-1]+'…'
    # Readable both as native plain text and Markdown. Neutralize markup syntax
    # with visible literal punctuation; retain exact originals in quoted details.
    value = re.sub(r'(?i)\bwww\.', 'www．', value).replace('://', ':／／').replace('@', '＠')
    return value.translate(str.maketrans({'<':'‹','>':'›','[':'［',']':'］',
        '`':'ˋ','*':'＊','_':'＿','~':'～','\\':'＼'}))



def quoted(value):
    # JSON escaping prevents embedded newlines from posing as control lines.
    # Readable Unicode (emoji, accents) is kept; bidi overrides stay escaped.
    data = json.dumps(value, ensure_ascii=False, indent=2)
    data = ''.join('\\u%04x' % ord(c) if ord(c) in BIDI else c for c in data)
    fence = '`' * max(3, 1+max((len(x) for x in re.findall(r'`+', data)), default=0))
    return fence+'json\n'+data+'\n'+fence


BIDI = dict.fromkeys(map(ord, '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'))


def readable(value):
    """Human text for the agent-only details: real Unicode, no control or bidi characters."""
    text = ''.join(c for c in str(value or '') if c in '\n\t' or unicodedata.category(c) != 'Cc').translate(BIDI)
    return ' '.join(text.split())


def quote_block(value, cap=20000):
    """Quote the original message line by line.

    Every line starts with '>' so no quoted line can pose as a control line
    (HANDOFF, Exact sender, the technical-details boundary). Readable Unicode is
    kept; only control and bidi-override characters are removed.
    """
    text = ''.join(c for c in str(value or '') if c in '\n\t' or unicodedata.category(c) != 'Cc').translate(BIDI)
    if len(text) > cap:
        text = text[:cap] + '\n… (truncated; the full text is the original workflow message)'
    lines = text.splitlines() or ['(empty)']
    return '\n'.join('> ' + line if line.strip() else '>' for line in lines)


def detail_lines(full):
    """Only what the 7-line header cannot show: the next step, untruncated text, the decision."""
    out = []
    if full.get('nextStep'): out.append('Next step: ' + readable(full['nextStep']))
    for key, label, shown in (('result', 'Full result', 72), ('remaining', 'Full remaining', 88)):
        value = readable(full.get(key))
        if len(value) > shown: out.append(label + ': ' + value)
    d = full.get('decision')
    if d:
        note = readable((d.get('record') or {}).get('note'))
        out.append('Decision: ' + readable(d.get('reviewer')) + ' ' + readable(d.get('action')) + (' Note: ' + note if note else ''))
    return '\n'.join(out)


def role(flow, actor, sessions=None):
    if actor == 'service:birds': return 'Birds deadline service'
    if actor == 'service:router': return 'Flow router service'
    sessions = sessions if sessions is not None else flow.identity_catalog()['sessions']
    label = flow.display_identity(actor, sessions)
    if not label or label == actor or label.startswith('codex:'): label = 'Agent (name unavailable)'
    return plain(label, 512)


def subject(body):
    first = str(body or '').splitlines()[0] if body else ''
    if not first or len(first) > 60 or re.search(r'\b[0-9a-f]{8}\b|/|\{|\}',first):
        return 'New assignment'
    return first


def fields(flow, sender, recipient, *, kind='assignment', body='', result=None, closure=None, decision_actor=None):
    sessions = flow.identity_catalog()['sessions']
    sender_name, name = role(flow, sender, sessions), role(flow, recipient, sessions)
    title, status = subject(body), 'Action requested'
    outcome, remaining, next_step = 'An assignment is ready to pick up.', 'The requested work still needs a result.', 'Read the assignment and take the authorized next step.'
    availability = 'Not established'
    decision = None
    if kind == 'return':
        result = result or {}
        p = result.get('presentation')
        p = validate(p) if p is not None else None
        title, status = 'Work update', 'Review needed'
        outcome = 'The assigned agent returned a result.'
        remaining, next_step = 'The result has not been accepted.', 'Review the evidence and record a decision.'
        if result.get('disposition') == 'BLOCKED':
            status, outcome = 'Blocked', 'The assigned agent reported a blocker.'
            remaining, next_step = 'The assignment is unfinished.', 'Review the blocker and resolve the dependency.'
        if p:
            title, outcome, remaining, next_step = (p[k] for k in ('title','result','remaining','nextStep'))
            availability = AVAILABILITY[p['availability']]
        if closure:
            status = {'accepted':'Review recorded','revision':'Changes needed','blocked':'Blocked'}.get(closure.get('outcome'),'Decision recorded')
            # A late-delivered return must not replay stale proposed next steps.
            reviewer = role(flow,decision_actor,sessions) if decision_actor else 'Reviewer (name unavailable)'
            action = {'accepted':'accepted the result.', 'revision':'requested changes.', 'blocked':'marked the work blocked.'}.get(closure.get('outcome'),'recorded a decision.')
            outcome = reviewer+' '+action
            decision = {'actor':decision_actor,'reviewer':reviewer,'action':action,'record':closure}
            remaining = 'Project completion and installation remain separate.'
            next_step = 'Follow the recorded decision; do not repeat this handoff.'
    elif kind == 'overdue':
        title, status = 'Work deadline', 'Needs attention'
        outcome = 'The deadline passed without a recorded sender decision.'
        remaining = 'This does not show whether the assigned agent finished.'
        next_step = 'Check the work and any return; resolve the blocker or record a decision.'
    return {'title':title,'status':status,'from':sender_name,'to':name,
            'nextStep':next_step,'result':outcome,'remaining':remaining,
            'availability':availability,'decision':decision}


def compact(value, budget):
    """Literal display excerpt only; full field is retained in technical context.

    Account conservatively for wide Unicode, capitals and wide Latin glyphs
    after neutralizing markup. The renderer-specific fit is checked in a browser. Shorten at a
    word boundary when possible; ellipsis advertises omission, never a summary.
    """
    text=plain(value,2048)
    def width(c):return 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') or c.isupper() or c in 'mw' else 1
    if sum(width(c) for c in text)<=budget:return text
    used=0;out=''
    for c in text:
        if used+width(c)>budget-1:break
        out+=c;used+=width(c)
    if ' ' in out and len(out.rsplit(' ',1)[0])>=len(out)//2:out=out.rsplit(' ',1)[0]
    return out.rstrip()+'…'


def lead(flow, sender, recipient, **kwargs):
    f=fields(flow,sender,recipient,**kwargs)
    outcome=f['result']
    if f['decision']:
        # Keep the actual actor and decision verb together even for a long role.
        outcome=compact(f['decision']['reviewer'],32)+' '+f['decision']['action']
    return ('Agent handoff: '+compact(f['title'],32)+' — '+f['status']+
            '\nFrom: '+compact(f['from'],32)+'\nTo: '+compact(f['to'],32)+
            '\nNext: '+compact(f['to'],32)+'\nResult: '+compact(outcome,72)+
            '\nRemaining: '+compact(f['remaining'],88)+'\nReported availability: '+f['availability'])


def full_context(flow, sender, recipient, **kwargs):
    # Plain data under the technical boundary, including the exact closure actor
    # and record. Formatting never mutates the authoritative work row.
    return fields(flow,sender,recipient,**kwargs)


def context(flow, mid):
    from workflow import workflow_reader
    from workflow_work import exists, alert_link, one
    import sqlite3
    with workflow_reader(flow.path) as db:
        db.row_factory = sqlite3.Row
        if not exists(db): return {}
        row = db.execute('SELECT * FROM workflow_work WHERE return_message_id=?',(mid,)).fetchone()
        if row:
            return {'kind':'return','result':json.loads(row['result']),
                    'closure':json.loads(row['closure']) if row['closure'] else None,
                    'decision_actor':one(db,row['message_id'])['sender']}
        link = alert_link(db,mid)
        if link: return {'kind':'overdue'}
    return {}


CARD_MARK = '🤝 '
CARD_PREFIXES = ('From ', 'Result: ', 'Summary: ', 'Still open: ', 'Next step: ', 'Availability: ', 'Decision: ', 'Message:', '>')


def card(flow, sender, recipient, *, body='', **kwargs):
    """Readable-first handoff card (v2). Only generated, prefix-labelled lines and '>'-quoted text."""
    f = fields(flow, sender, recipient, body=body, **kwargs)
    kind = kwargs.get('kind', 'assignment')
    lines = [CARD_MARK + '**' + plain(f['title'], 80) + '** — ' + f['status'],
             'From ' + plain(f['from'], 60) + ' → ' + plain(f['to'], 60)]
    if kind != 'assignment':
        # Assignments carry only fixed filler here; the message itself says what to do.
        lines += ['', 'Result: ' + plain(f['result'], 400)]
        summary = ((kwargs.get('result') or {}).get('summary') if kind == 'return' else None)
        if summary: lines.append('Summary: ' + plain(summary, 600))
        lines.append('Still open: ' + plain(f['remaining'], 300))
        lines.append('Next step: ' + plain(f['nextStep'], 300))
    if f['availability'] != 'Not established': lines.append('Availability: ' + f['availability'])
    if f['decision']:
        note = plain((f['decision'].get('record') or {}).get('note'), 300)
        lines.append('Decision: ' + plain(f['decision']['reviewer'], 60) + ' ' + f['decision']['action'] + (' Note: ' + note if note else ''))
    if kind == 'assignment':
        # Break Markdown image syntax so quoted text can never pull a remote image into the UI.
        lines += ['', 'Message:', quote_block(str(body or '').replace('![', '!\u200b['))]
    return '\n'.join(lines)


def _card_v2_head(head):
    lines = head.splitlines()
    return (len(head) <= 60000 and len(lines) >= 2 and lines[0].startswith(CARD_MARK) and lines[1].startswith('From ')
            and all(line == '' or line.startswith(CARD_PREFIXES) for line in lines[2:]))


def marker_matches(body, request_id):
    """Accept old first-line markers and the bounded v1 human envelope only.

    Never accept a quoted marker or arbitrary substring. User text is serialized
    below this control boundary by the formatter. Both observer paths share this.
    """
    if not isinstance(body,str): return False
    if body.startswith('HANDOFF '+request_id+'\n'): return True
    head, separator, tail = body.partition(DETAILS)
    if separator and _card_v2_head(head):
        return tail.startswith('HANDOFF '+request_id+'\nExact sender: ')
    lines = head.splitlines()
    return bool(separator and len(head) <= 2400 and len(lines) == 7 and
                lines[0].startswith('Agent handoff: ') and
                all(line.startswith(prefix) for line,prefix in zip(lines[1:],
                    ('From: ','To: ','Next: ','Result: ','Remaining: ','Reported availability: '))) and
                tail.startswith('HANDOFF '+request_id+'\nExact sender: '))
