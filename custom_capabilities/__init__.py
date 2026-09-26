"""Drop-in read-only Switchboard routes. Import has no state-store writes."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from .catalog import build_catalog

ROOT = Path(__file__).parent
ASSETS = {name: (ROOT / name).read_bytes() for name in ("index.html", "style.css", "app.js", "nav.js", "header.css")}
ASSET_TYPES = {"html": "text/html", "css": "text/css", "js": "text/javascript"}


def source_hashes():
    result = {"custom_capabilities/" + k: hashlib.sha256(v).hexdigest() for k, v in ASSETS.items()}
    for name in ("__init__.py", "catalog.py"):
        result["custom_capabilities/" + name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def route(handler, catalog_factory=build_catalog):
    """Return True only for exact owned GET routes; callers retain all others."""
    path = urlparse(handler.path).path
    asset = "index.html" if path in ("/capabilities", "/capabilities/") else path.removeprefix("/capabilities/")
    if path != "/api/capabilities" and not (path.startswith("/capabilities") and asset in ASSETS):
        return False
    expected = ("127.0.0.1:" + str(handler.server.server_port), "localhost:" + str(handler.server.server_port))
    if handler.headers.get("Host") not in expected:
        body, content_type, status = b'{"error":"Loopback host required"}', "application/json", 403
    elif path == "/api/capabilities":
        try:
            body, content_type, status = json.dumps(catalog_factory(), ensure_ascii=False).encode(), "application/json", 200
        except Exception:
            body, content_type, status = b'{"error":"Custom capability inventory is unavailable. Retry to scan again."}', "application/json", 503
    else:
        body, content_type, status = ASSETS[asset], ASSET_TYPES[asset.rsplit(".", 1)[1]], 200
    handler.send_response(status)
    handler.send_header("Content-Type", content_type + "; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
    handler.end_headers()
    handler.wfile.write(body)
    return True
