"""Shared fixtures: temporary workspaces, a fake publisher web server, sample PDFs."""

from __future__ import annotations

import http.server
import shutil
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

FAKE_PDF = b"%PDF-1.4\n" + b"%" + b"x" * 8000 + b"\n%%EOF\n"


def pdf_bytes(tag: str = "") -> bytes:
    return FAKE_PDF.replace(b"x" * len(tag.encode()), tag.encode(), 1) if tag else FAKE_PDF


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.litsuite, keyring or network config during tests."""
    home = tmp_path / "litsuite_home"
    monkeypatch.setenv("LITSUITE_HOME", str(home))
    import litsuite.config as cfg
    import litsuite.secrets as sec

    monkeypatch.setattr(cfg, "APP_HOME", home)
    monkeypatch.setattr(sec, "SECRETS_FILE", home / "secrets.json")
    monkeypatch.setattr(sec, "TOKENS_DIR", home / "tokens")
    monkeypatch.setattr(sec, "_keyring", lambda: None)
    return home


@pytest.fixture
def library(tmp_path):
    """A small library folder with a Surveys subfolder and an _old folder."""
    lib = tmp_path / "Literature"
    (lib / "Surveys").mkdir(parents=True)
    (lib / "_old").mkdir()
    for name in ["ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf",
                 "ACCESS 25 Fryganiotis - Model_Predictive_Control_based_Dynamic_RA_Enabler_for_Resilient_NS.pdf",
                 "ICNC 24 Raftopoulos - DRL-Based_Latency-Aware_NS_in_O-RAN_with_Time-Varying_SLAs.pdf",
                 "Surveys/COMST 22 Wu - A_Survey_on_NS_Enabling_Technologies.pdf",
                 "_old/TVT 19 - Obsolete_draft.pdf"]:
        (lib / name).write_bytes(FAKE_PDF)
    (lib / "notes.txt").write_text("not a pdf")
    return lib


class _Handler(http.server.BaseHTTPRequestHandler):
    log_message = lambda *a, **k: None  # noqa: E731

    def _send(self, code, body=b"", ctype="text/html", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _logged_in(self):
        return "session=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        self.server.hits.append(path)
        if path.startswith("/oa/"):
            return self._send(200, pdf_bytes("OA"), "application/pdf")
        if path.startswith("/article/"):
            aid = path.rsplit("/", 1)[1]
            html = (f'<html><head><meta name="citation_pdf_url" content="/pdf/{aid}"></head>'
                    f'<body><h1>Article {aid}</h1></body></html>')
            return self._send(200, html.encode())
        if path.startswith("/stamp/"):
            aid = path.rsplit("/", 1)[1]
            return self._send(200, f'<html><body><iframe src="/pdf/{aid}"></iframe></body></html>'.encode())
        if path.startswith("/pdf/") or path.startswith("/download/"):
            if not self._logged_in():
                return self._send(302, b"", headers={"Location": f"/login?next={urllib.parse.quote(path)}"})
            extra = {"Content-Disposition": 'attachment; filename="paper.pdf"'} if path.startswith("/download/") else {}
            return self._send(200, pdf_bytes("PAYWALL"), "application/pdf", extra)
        if path == "/login":
            nxt = (qs.get("next") or ["/home"])[0]
            html = (f'<html><body><form method="post" action="/login?next={urllib.parse.quote(nxt)}">'
                    '<input id="username" name="username"><input id="password" name="password" type="password">'
                    '<button type="submit">Sign in</button></form></body></html>')
            return self._send(200, html.encode())
        if path == "/home":
            text = "Access provided by: Example University" if self._logged_in() else "Institutional Sign In"
            return self._send(200, f"<html><body><p>{text}</p></body></html>".encode())
        return self._send(404, b"not found")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if url.path == "/login" and form.get("username") == ["alice"] and form.get("password") == ["secret"]:
            nxt = (urllib.parse.parse_qs(url.query).get("next") or ["/home"])[0]
            return self._send(302, b"", headers={"Location": nxt, "Set-Cookie": "session=ok; Path=/; Max-Age=86400"})
        return self._send(403, b"bad credentials")


@pytest.fixture(scope="session")
def publisher_server():
    socketserver.TCPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.hits = []
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server
    server.shutdown()


@pytest.fixture(autouse=True)
def no_proxy_for_localhost(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.fixture
def emails_dir(tmp_path, publisher_server):
    """Copy of the fixture emails with the fake server's port filled in."""
    base, _ = publisher_server
    port = base.rsplit(":", 1)[1]
    out = tmp_path / "emails"
    out.mkdir()
    for f in (FIXTURES / "emails").glob("*.eml"):
        (out / f.name).write_bytes(f.read_bytes().replace(b"PORT", port.encode()))
    return out


@pytest.fixture
def copy_config(tmp_path):
    def _copy(text: str) -> Path:
        cfg = tmp_path / "repo" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        for sub in ("abbreviations", "venues"):
            shutil.copytree(ROOT / "config" / sub, cfg / sub, dirs_exist_ok=True)
        path = cfg / "settings.yaml"
        path.write_text(text, encoding="utf-8")
        return path
    return _copy
