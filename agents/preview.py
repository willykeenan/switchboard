"""Isolated preview; no board imports, stores, writes or process controls."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .http import AgentsRoutes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=47846)
    args = parser.parse_args()
    routes = AgentsRoutes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if routes.handle_get(self):
                return
            if self.path == '/constellations' or self.path.startswith('/rooms'):
                self.send_response(307)
                self.send_header('Location', 'http://127.0.0.1:47834'+self.path)
                self.end_headers()
                return
            self.send_error(404)

        def do_POST(self):
            self.send_error(405, 'Read only')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    print(f'Agents preview: http://127.0.0.1:{server.server_port}/agents', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
