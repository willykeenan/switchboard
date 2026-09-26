"""Small GET-only integration point for the existing Switchboard HTTP owner."""
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse
import json

from .observer import Observer

ROOT = Path(__file__).resolve().parent


class AgentsRoutes:
    def __init__(self, observer=None):
        self.observer = observer or Observer()
        self.assets = {name: (ROOT/name).read_bytes() for name in ('agents.html', 'agents.css', 'agents.js')}

    def handle_get(self, handler):
        path = urlparse(handler.path).path
        if path not in ('/agents', '/agents/', '/agents.css', '/agents.js', '/api/agents'):
            return False
        allowed = {f'127.0.0.1:{handler.server.server_port}', f'localhost:{handler.server.server_port}'}
        origin = handler.headers.get('Origin')
        if handler.headers.get('Host') not in allowed or handler.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and origin not in {'http://'+a for a in allowed}):
            self.reply(handler, b'{"error":"Local origin required"}', 'application/json', HTTPStatus.FORBIDDEN)
            return True
        if path == '/api/agents':
            try:
                body = json.dumps(self.observer.snapshot(), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
                self.reply(handler, body, 'application/json')
            except Exception:
                self.reply(handler, b'{"error":"Worker observer unavailable"}', 'application/json', HTTPStatus.SERVICE_UNAVAILABLE)
            return True
        name = 'agents.html' if path in ('/agents', '/agents/') else path[1:]
        self.reply(handler, self.assets[name], {'html': 'text/html', 'css': 'text/css', 'js': 'text/javascript'}[name.rsplit('.',1)[1]])
        return True

    @staticmethod
    def reply(handler, body, content_type, status=HTTPStatus.OK):
        handler.send_response(status)
        handler.send_header('Content-Type', content_type+'; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
