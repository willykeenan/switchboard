"""Shared settings HTTP adapter. No identity can be asserted by request JSON.

The host must inject authenticate(handler)->Principal using a verified identity
broker. The installed board currently has no such broker; default writes deny.
"""
import json
import secrets
from urllib.parse import urlparse, parse_qs
from agent_settings import Denied, Unavailable
from workspace import Conflict

class SettingsRoutes:
    def __init__(self, settings, token, authenticate=None):
        self.settings, self.token = settings, token
        self.authenticate = authenticate or (lambda handler: None)

    def handle(self, h, method):
        url=urlparse(h.path)
        if url.path not in ('/api/agent-settings','/api/agent-settings/models'): return False
        origin='http://'+h.headers.get('Host','')
        if h.headers.get('Host') not in (f'127.0.0.1:{h.server.server_port}',f'localhost:{h.server.server_port}') or h.headers.get('Sec-Fetch-Site')=='cross-site' or (h.headers.get('Origin') and h.headers.get('Origin')!=origin):
            h.send_json({'error':'Same-origin loopback access required'},403); return True
        try:
            principal=self.authenticate(h)
            if method=='GET':
                if url.path.endswith('/models'):
                    self.settings.production.models()  # supported account/model read, never thread/start or turn/start
                    result=self.settings.models()
                else: result=self.settings.read(parse_qs(url.query).get('agent',[''])[0],principal)
            elif method=='POST' and url.path=='/api/agent-settings':
                if h.headers.get('Origin')!=origin or not secrets.compare_digest(h.headers.get('X-KE-Board-Token',''),self.token()): raise Denied('Same-origin CSRF token required in addition to authenticated identity')
                if h.headers.get('Transfer-Encoding') or h.headers.get('Content-Type')!='application/json': raise ValueError('Send a bounded JSON body')
                size=int(h.headers.get('Content-Length','0'))
                if not 0<size<=20000: raise ValueError('Invalid settings request length')
                result=self.settings.update(json.loads(h.rfile.read(size)),principal)
            else:
                h.send_json({'error':'Method not supported'},405); return True
            h.send_json(result)
        except Denied as exc: h.send_json({'error':str(exc),'code':'SETTINGS_AUTH_REQUIRED'},403)
        except Conflict as exc: h.send_json({'error':str(exc),'code':'SETTINGS_CONFLICT'},409)
        except (ValueError,TypeError,KeyError) as exc: h.send_json({'error':str(exc),'code':'SETTINGS_INVALID'},400)
        except Exception: h.send_json({'error':'Settings or authenticated catalog unavailable. Saved values are preserved.','code':'SETTINGS_UNAVAILABLE'},503)
        return True
