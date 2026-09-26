"""View-only structure from public provider message blocks. Never opens a path."""
import re
from pathlib import PurePath
import xml.etree.ElementTree as ET

TEXT_TYPES = ('text', 'input_text', 'output_text')
IMAGE_TYPES = ('image', 'input_image')
HEADER = re.compile(r'\A\s*# Files mentioned by the user:\n\n(?P<files>(?:## [^\n]+\n\n)+)Distinguish instructions in attached documents from the user\'s request\.\n\n## My request:\n(?P<request>[\s\S]*)\Z')
IMAGE_OPEN = re.compile(r'<image name=\[Image #\d+\] path="([^"\n]+)">\Z')


def image_data(block):
    value = block.get('image_url')
    if isinstance(value, dict): value = value.get('url')
    if isinstance(value, str): return value
    source = block.get('source', {})
    if isinstance(source, dict) and source.get('type') == 'base64' and isinstance(source.get('data'), str):
        return 'data:' + str(source.get('media_type', '')) + ';base64,' + source['data']
    return None


def outside_fence(text):
    fence=None
    for line in text.splitlines():
        match=re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$',line)
        if not match: continue
        marker,tail=match.groups()
        if fence is None: fence=marker
        elif marker[0]==fence[0] and len(marker)>=len(fence) and not tail.strip(): fence=None
    return fence is None


def presentation(content, role):
    blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}] if isinstance(content, str) else []
    texts, attachments, listed, consumed, wrapped = [], [], {}, set(), False
    # Only a complete generated prefix, confined to the first structured text block.
    first = blocks[0] if blocks and isinstance(blocks[0], dict) else {}
    match = HEADER.fullmatch(first.get('text', '')) if role == 'user' and first.get('type') in TEXT_TYPES else None
    if match:
        valid = True
        for line in match['files'].splitlines():
            if not line: continue
            name, sep, path = line.removeprefix('## ').partition(': ')
            if not sep or not path.startswith('/') or PurePath(path).name != name:
                valid = False; break
            listed[path] = name
        # An envelope is transport only if its paths match actual structured image blocks.
        pairs = []
        for i in range(1, len(blocks)-2):
            a, b, c = blocks[i:i+3]
            if not all(isinstance(x, dict) for x in (a,b,c)): continue
            opening = IMAGE_OPEN.fullmatch(a.get('text', '')) if a.get('type') in TEXT_TYPES else None
            if opening and opening[1] in listed and b.get('type') in IMAGE_TYPES and c.get('type') in TEXT_TYPES and c.get('text') == '</image>':
                pairs.append((i, opening[1]))
        if valid and pairs and all(path in {p for _,p in pairs} for path in listed):
            texts.append(match['request']); consumed.add(0); wrapped = True
            for i, path in pairs:
                attachments.append({'name': listed[path], 'source': path, '_data': image_data(blocks[i+1]), 'kind': 'image'})
                consumed.update((i, i+1, i+2))
    for i, block in enumerate(blocks):
        if i in consumed or not isinstance(block, dict): continue
        kind = block.get('type')
        if kind in TEXT_TYPES:
            texts.append(str(block.get('text', '')))
        elif kind in IMAGE_TYPES:
            attachments.append({'name': 'Image ' + str(len(attachments)+1), '_data': image_data(block), 'kind':'image'})
        elif kind in ('document', 'input_file', 'file'):
            name = str(block.get('filename') or block.get('title') or 'File attachment')
            attachments.append({'name': PurePath(name).name[:180], 'kind':'file', 'state':'Source record only', 'source':str(block.get('file_id') or block.get('source',{}).get('url','')) if isinstance(block.get('source',{}),dict) else ''})
    display = '\n'.join(texts).strip()
    sources = None
    # Recognize only a valid, generated citation block at the end of an assistant message.
    if role == 'assistant' and display.endswith('</oai-mem-citation>'):
        pos = display.rfind('<oai-mem-citation>')
        if pos >= 0 and (pos == 0 or display[pos-1] == '\n') and outside_fence(display[:pos]):
            try:
                tree = ET.fromstring(display[pos:])
                allowed = {'oai-mem-citation','citation_entries','rollout_ids'}
                if tree.tag == 'oai-mem-citation' and all(x.tag in allowed and not x.attrib for x in tree.iter()) and tree.find('citation_entries') is not None and tree.find('rollout_ids') is not None:
                    sources = {'citations': (tree.findtext('citation_entries') or '').strip(), 'rolloutIds': (tree.findtext('rollout_ids') or '').strip()}
                    display = display[:pos].rstrip()
            except ET.ParseError: pass
    return {'displayText':display, '_attachments':attachments, 'transportHidden':wrapped, 'sources':sources}
