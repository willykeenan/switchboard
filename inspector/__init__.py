"""Local transcript windows and project evidence library."""
from pathlib import Path
from urllib.parse import urlparse,parse_qs,quote
import hashlib,hmac,json
from .transcripts import Transcripts
from .library import Library
from .messages import Messages
from .current_work import CurrentWork
from .role_panels import RolePanels
from workflow import workflow_reader
from . import context as team_context
from .review_records import reviews
class Inspector:
    def __init__(self,root,workflow,token,library_directory=None):
        self.messages=Messages(workflow);self.transcripts=Transcripts(workflow);self.library=Library(root,workflow,library_directory);self.token=token
        self.current_work=CurrentWork(workflow.path, workflow_reader)
        self.role_panels=RolePanels(workflow.path, workflow_reader)
        self.assets={p.name:p.read_bytes() for p in Path(__file__).parent.iterdir() if p.suffix in ('.html','.js','.css')}
    def hashes(self):return {'inspector/'+p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.iterdir() if p.suffix in ('.py','.html','.js','.css') or p.name=='role-registry.json'}
    def allowed(self,h):
        hosts=(f'127.0.0.1:{h.server.server_port}',f'localhost:{h.server.server_port}')
        return h.headers.get('Host') in hosts and h.headers.get('Sec-Fetch-Site')!='cross-site'
    def get(self,h):
        url=urlparse(h.path);path=url.path;query=parse_qs(url.query,keep_blank_values=True);get=lambda k,d='':query.get(k,[d])[0]
        file={'/session':'session.html','/library':'library.html'}.get(path,path.removeprefix('/inspector/') if path.startswith('/inspector/') else '')
        if file not in self.assets and path not in ('/api/role-panels','/api/service-displays/current-work','/api/reviews','/api/transcript/attachment','/api/transcript','/api/library','/api/library/item','/api/messages','/api/library/context','/api/library/workspace'):return False
        if not self.allowed(h):h.send_json({'error':'Loopback access required'},403);return True
        try:
            if path=='/api/role-panels':
                if len(h.path)>2048:raise ValueError('Bounded request required')
                h.send_json(self.role_panels.get(query));return True
            if path=='/api/service-displays/current-work':
                if len(h.path)>2048:raise ValueError('Bounded request required')
                h.send_json(self.current_work.get(query))
            elif path=='/api/transcript/attachment':
                body,mime,name=self.transcripts.attachment(get('agent'),get('token'))
                h.send_response(200);h.send_header('Content-Type',mime);h.send_header('Content-Length',str(len(body)));h.send_header('Cache-Control','no-store');h.send_header('X-Content-Type-Options','nosniff');h.send_header('Content-Security-Policy',"default-src 'none'; sandbox");h.send_header('Content-Disposition',"inline; filename*=UTF-8''"+quote(name,safe=''));h.end_headers();h.wfile.write(body)
            elif file in self.assets:
                body=self.assets[file];h.send_response(200);h.send_header('Content-Type',{'html':'text/html','js':'application/javascript','css':'text/css'}[file.rsplit('.',1)[1]]+'; charset=utf-8');h.send_header('Content-Length',str(len(body)));h.send_header('Cache-Control','no-store');h.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'");h.send_header('X-Content-Type-Options','nosniff');h.end_headers();h.wfile.write(body)
            else:
                result=__import__('inspector.lifecycle',fromlist=['snapshot']).snapshot(self.library,get('project'),get('lane'),get('team')) if path=='/api/library/workspace' else reviews(get('project')) if path=='/api/reviews' else team_context.context(self.library,get('project'),get('lane'),get('team','researcher' if get('lane') else '')) if path=='/api/library/context' else self.messages.page(get('agent'),get('lane'),get('before')) if path=='/api/messages' else self.transcripts.page(get('agent'),int(get('before','0')),reader=get('reader'),initialized=get('initialized')=='1') if path=='/api/transcript' else self.library.item(get('id'),get('version'),get('project'),get('lane'),get('team')) if path.endswith('/item') else self.library.query(get('project'),get('lane'),get('q'),get('kind'),int(get('offset','0')),get('team'))
                h.send_json(result)
        except (ValueError,KeyError,TypeError) as e:h.send_json({'error':str(e)},400)
        except Exception:h.send_json({'error':'This local record is temporarily unavailable. Retry to reload it.'},503)
        return True
    def post(self,h):
        path=urlparse(h.path).path
        if path not in ('/api/library/refresh','/api/library/note','/api/library/review','/api/library/context','/api/library/lifecycle'):return False
        origin='http://'+h.headers.get('Host','')
        if not self.allowed(h) or h.headers.get('Origin')!=origin or not hmac.compare_digest(h.headers.get('X-KE-Board-Token',''),self.token()):h.send_json({'error':'Local control authorization required'},403);return True
        try:
            size=int(h.headers.get('Content-Length','0'))
            if not 0<size<=150000:raise ValueError('Invalid request size')
            item=json.loads(h.rfile.read(size))
            result=__import__('inspector.lifecycle',fromlist=['write']).write(self.library,item) if path=='/api/library/lifecycle' else team_context.save(self.library,item) if path=='/api/library/context' else self.library.refresh(True) if path.endswith('/refresh') else self.library.write(path.rsplit('/',1)[1],item)
            h.send_json(result)
        except (ValueError,KeyError,TypeError) as e:h.send_json({'error':str(e)},400)
        except Exception:h.send_json({'error':'The library could not save this change. Your source files are unchanged.'},503)
        return True
