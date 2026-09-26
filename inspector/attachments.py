"""Bounded inline images, authorized by exact public message bytes, never by paths."""
import base64, hashlib, hmac, json, secrets, struct
from urllib.parse import quote
MAX_IMAGE = 12 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_RECORD = 20 * 1024 * 1024


def decode_image(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE * 4 // 3 + 100: raise ValueError('Image exceeds the preview limit')
    head, comma, encoded = value.partition(',')
    types = {'data:image/png;base64':'image/png', 'data:image/jpeg;base64':'image/jpeg', 'data:image/gif;base64':'image/gif', 'data:image/webp;base64':'image/webp'}
    mime = types.get(head)
    if not comma or not mime: raise ValueError('No supported embedded image is available')
    try: body = base64.b64decode(encoded, validate=True)
    except Exception: raise ValueError('The embedded image is invalid') from None
    if not 0 < len(body) <= MAX_IMAGE: raise ValueError('Image exceeds the preview limit')
    width = height = 0
    if mime == 'image/png' and len(body) >= 24 and body[:8] == b'\x89PNG\r\n\x1a\n' and body[12:16] == b'IHDR':
        width,height = struct.unpack('>II',body[16:24])
    elif mime == 'image/gif' and len(body) >= 10 and body[:6] in (b'GIF87a',b'GIF89a'):
        width,height = struct.unpack('<HH',body[6:10])
    elif mime == 'image/jpeg' and body[:2] == b'\xff\xd8':
        i = 2
        while i+4 < len(body):
            if body[i] != 255: break
            while i < len(body) and body[i] == 255: i += 1
            if i >= len(body): break
            marker = body[i]; i += 1
            if marker in (0xD8,0xD9) or 0xD0 <= marker <= 0xD7: continue
            if marker == 0xDA: break
            size = int.from_bytes(body[i:i+2],'big')
            if size < 2 or i+size > len(body): break
            if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF) and size >= 7:
                height,width = struct.unpack('>HH',body[i+3:i+7]); break
            i += size
    elif mime == 'image/webp' and len(body) >= 30 and body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        kind = body[12:16]
        if kind == b'VP8X': width,height = 1+int.from_bytes(body[24:27],'little'),1+int.from_bytes(body[27:30],'little')
        elif kind == b'VP8 ' and body[23:26] == b'\x9d\x01\x2a': width,height = (int.from_bytes(body[26:28],'little')&0x3fff),(int.from_bytes(body[28:30],'little')&0x3fff)
        elif kind == b'VP8L' and body[20] == 0x2f:
            bits = int.from_bytes(body[21:25],'little');width,height = (bits&0x3fff)+1,((bits>>14)&0x3fff)+1
    if not width or not height or width*height > MAX_PIXELS or max(width,height)>16000: raise ValueError('Image dimensions are invalid or exceed the preview limit')
    return body,mime,width,height


class AttachmentTokens:
    def __init__(self): self.secret = secrets.token_bytes(32)
    def issue(self, payload):
        raw = base64.urlsafe_b64encode(json.dumps(payload,separators=(',',':')).encode()).decode().rstrip('=')
        return raw+'.'+hmac.new(self.secret,raw.encode(),hashlib.sha256).hexdigest()
    def verify(self, token, agent):
        try:
            if not isinstance(token,str) or len(token)>2048: raise ValueError()
            raw,signature = token.split('.')
            if not hmac.compare_digest(signature,hmac.new(self.secret,raw.encode(),hashlib.sha256).hexdigest()): raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
            if data['agent'] != agent or not 0 <= data['offset'] or not 0 < data['length'] <= MAX_RECORD: raise ValueError()
            return data
        except (ValueError,KeyError,TypeError): raise ValueError('Attachment does not belong to this selected message') from None
    def describe(self, spec, payload):
        result = {k:v for k,v in spec.items() if not k.startswith('_')}
        if spec['kind'] != 'image': return result
        try:
            body,mime,width,height = decode_image(spec.get('_data'))
            result.update(state='Available',mime=mime,width=width,height=height,bytes=len(body),url='/api/transcript/attachment?agent='+quote(payload['agent'],safe='')+'&token='+quote(self.issue(payload),safe=''))
        except ValueError as e: result.update(state='Unavailable',reason=str(e))
        return result
