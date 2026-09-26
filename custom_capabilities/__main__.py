"""Read-only global CLI; preview mode has no board imports or state-store access."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import route
from .catalog import build_catalog

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--serve", type=int, metavar="PORT", help="Serve a loopback-only independent preview")
parser.add_argument("--compact", action="store_true", help="Print command, purpose and source for each capability")
args = parser.parse_args()
if args.serve:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not route(self):
                self.send_error(404)
        def do_POST(self):
            self.send_error(405)
    server = ThreadingHTTPServer(("127.0.0.1", args.serve), Handler)
    print("Custom capability preview: http://127.0.0.1:%d/capabilities" % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
else:
    catalog = build_catalog()
    if args.compact:
        print(catalog["scopeNote"])
        print(catalog["accessNote"])
        for entry in catalog["capabilities"]:
            print("%s — %s [%s, %s]\n  %s\n  %s" % (entry["invocation"], entry["title"], entry["scope"], entry["state"], entry["summary"], entry["sources"][0]["path"]))
        for warning in catalog["warnings"]:
            print("INCOMPLETE: " + warning)
    else:
        print(json.dumps(catalog, ensure_ascii=False, indent=2))
