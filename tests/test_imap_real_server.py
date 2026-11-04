"""The IMAP provider against a real IMAP4rev1 server (pymap), over STARTTLS and
implicit TLS, with certificate verification. Proves the unread-only rule on the
server side: searching never sets \\Seen; only mark_as_read does."""

import asyncio
import datetime as dt
import imaplib
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

from conftest import FIXTURES

from litsuite.mail.imap import ImapMailbox

pytestmark = pytest.mark.skipif(not shutil.which("pymap") or not shutil.which("openssl"),
                                reason="needs pymap and openssl")

USER, PASSWORD = "alice", "secret"
SENDERS = ["scholaralerts-noreply@google.com", "@notification.elsevier.com"]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "key.pem"),
                    "-out", str(d / "cert.pem"), "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], check=True, capture_output=True)
    return d / "cert.pem", d / "key.pem"


@pytest.fixture
def imap_server(tls_files):
    """A fresh pymap server (STARTTLS) and an implicit-TLS front end, seeded with emails."""
    cert, key = tls_files
    port = _free_port()
    proc = subprocess.Popen(["pymap", "--host", "127.0.0.1", "--port", str(port), "--cert", str(cert),
                             "--key", str(key), "dict", "--demo-user", USER, "--demo-password", PASSWORD],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    # Implicit TLS (port-993 style) front end that forwards to pymap.
    tls_port = _free_port()
    loop = asyncio.new_event_loop()
    sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    sctx.load_cert_chain(cert, key)

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            w.close()

    async def handle(cr, cw):
        ur, uw = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))

    async def start():
        return await asyncio.start_server(handle, "127.0.0.1", tls_port, ssl=sctx)

    server = loop.run_until_complete(start())
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    # Seed the INBOX through IMAP APPEND, with flags and internal dates.
    now = dt.datetime.now(dt.timezone.utc)
    c = imaplib.IMAP4("127.0.0.1", port)
    c.login(USER, PASSWORD)
    seed = [("scholar_alert.eml", "", now), ("sciencedirect_alert.eml", "", now),
            ("personal_ieee.eml", "", now),                      # not an alert sender
            ("scholar_alert.eml", "\\Seen", now),                # an alert the user already read
            ("arxiv_digest.eml", "", now - dt.timedelta(days=60))]  # unread but too old
    for i, (name, flags, when) in enumerate(seed):
        raw = (FIXTURES / "emails" / name).read_bytes().replace(b"scholar-0001", f"scholar-{i}".encode())
        c.append("INBOX", f"({flags})" if flags else None, imaplib.Time2Internaldate(when), raw)
    c.logout()
    yield {"port": port, "tls_port": tls_port, "cert": cert}
    loop.call_soon_threadsafe(server.close)
    loop.call_soon_threadsafe(loop.stop)
    proc.terminate()
    proc.wait(timeout=10)


def _flags(port):
    c = imaplib.IMAP4("127.0.0.1", port)
    c.login(USER, PASSWORD)
    c.select("INBOX", readonly=True)
    _, data = c.fetch("1:*", "(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    c.logout()
    out = {}
    for item in data:
        if isinstance(item, tuple):
            mid = item[1].decode().split(":", 1)[1].strip()
            out[mid] = "\\Seen" in item[0].decode()
    return out


def _cfg(server, security, port_key):
    return {"name": "uni", "host": "127.0.0.1", "port": server[port_key], "username": USER, "security": security}


@pytest.mark.parametrize("security,port_key", [("starttls", "port"), ("ssl", "tls_port")])
def test_unread_only_search_and_mark_read(imap_server, security, port_key, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(imap_server["cert"]))    # trust the test server's certificate
    before = _flags(imap_server["port"])
    mb = ImapMailbox(_cfg(imap_server, security, port_key), password=PASSWORD)
    msgs = list(mb.search(SENDERS, dt.date.today() - dt.timedelta(days=14)))
    assert sorted(m.id for m in msgs) == ["<scholar-0@google.com>", "<sd-0001@notification.elsevier.com>"]
    assert _flags(imap_server["port"]) == before                     # reading changed nothing
    assert mb.mark_read(msgs) == 2
    mb.close()
    after = _flags(imap_server["port"])
    assert after["<scholar-0@google.com>"] and after["<sd-0001@notification.elsevier.com>"]
    assert not after["<personal-0001@ieee.org>"]                     # other mail untouched
    # Nothing unread is left from the alert senders.
    mb = ImapMailbox(_cfg(imap_server, security, port_key), password=PASSWORD)
    assert list(mb.search(SENDERS, dt.date.today() - dt.timedelta(days=14))) == []
    mb.close()


def test_certificate_is_verified(imap_server, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    import os

    monkeypatch.setattr(ssl, "create_default_context",
                        lambda *a, **k: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))  # no trusted CAs
    with pytest.raises(ssl.SSLCertVerificationError):
        ImapMailbox(_cfg(imap_server, "ssl", "tls_port"), password=PASSWORD)
    assert os.environ.get("SSL_CERT_DIR") == "/nonexistent"


def test_wrong_password_and_login_command(imap_server, monkeypatch, copy_config):
    import yaml

    from litsuite import login
    from litsuite.config import Settings
    from litsuite.secrets import get_secret

    monkeypatch.setenv("SSL_CERT_FILE", str(imap_server["cert"]))
    path = copy_config(yaml.safe_dump({"email_accounts": {"uni": {
        "provider": "imap", "host": "127.0.0.1", "port": imap_server["port"], "username": USER,
        "security": "starttls"}}}))
    settings = Settings.load(path)
    with pytest.raises(imaplib.IMAP4.error):
        login.login_email(settings, "uni", password_fn=lambda _="": "wrong", printer=lambda *a: None)
    assert get_secret("imap:uni") is None                             # a bad password is never saved
    msg = login.login_email(settings, "uni", password_fn=lambda _="": PASSWORD, printer=lambda *a: None)
    assert "Connected" in msg and get_secret("imap:uni") == PASSWORD
    # The stored password is used from now on.
    from litsuite.mail import open_mailbox

    mb = open_mailbox(settings.email_account("uni"))
    assert len(list(mb.search(SENDERS, dt.date.today() - dt.timedelta(days=14)))) == 2
    mb.close()
    assert "ready" in "\n".join(login.status_lines(settings))


def test_alert_pipeline_over_real_imap(imap_server, monkeypatch, copy_config, library, publisher_server):
    """Full run: unread alerts from IMAP -> PDFs in the library; alerts then marked read."""
    import yaml

    import litsuite.alerts as alerts
    from litsuite.alerts import run_alerts
    from litsuite.catalog import scan_libraries, update_catalog
    from litsuite.config import Settings
    from litsuite.secrets import set_secret
    from litsuite.storage import LocalStorage
    from test_downloads_and_alerts import LocalOnlyHttp, _meta_fakes

    base, _ = publisher_server
    for name, fn in _meta_fakes(base).items():
        monkeypatch.setattr(alerts, name, fn)
    monkeypatch.setenv("SSL_CERT_FILE", str(imap_server["cert"]))
    # The Scholar fixture's PDF link points at the local publisher.
    c = imaplib.IMAP4("127.0.0.1", imap_server["port"])
    c.login(USER, PASSWORD)
    c.select("INBOX")
    c.store("1:*", "+FLAGS", "(\\Deleted)")
    c.expunge()
    port = base.rsplit(":", 1)[1].encode()
    for name in ("scholar_alert.eml", "sciencedirect_alert.eml", "personal_ieee.eml"):
        c.append("INBOX", None, imaplib.Time2Internaldate(time.time()),
                 (FIXTURES / "emails" / name).read_bytes().replace(b"PORT", port))
    c.logout()
    set_secret("imap:uni", PASSWORD)
    path = copy_config(yaml.safe_dump({
        "projects": {"slicing": {
            "library": str(library), "recursive": True, "exclude_folders": ["_old"],
            "abbreviations": ["config/abbreviations/network_slicing.yaml"], "venue_doi_styles": {"10.1016": "j{code}"},
            "keywords": {"focus": ["network slicing", "resource allocation", "multi-agent reinforcement learning"],
                         "secondary": ["O-RAN", "admission control"], "exclude": ["protein"]},
            "alerts": {"accounts": ["uni"], "mode": "auto", "download_folder": "_new"}}},
        "email_accounts": {"uni": {"provider": "imap", "host": "127.0.0.1", "port": imap_server["port"],
                                   "username": USER, "security": "starttls", "mark_as_read": True}}}))
    settings = Settings.load(path)
    project = settings.project("slicing")
    update_catalog(project.catalog_path, scan_libraries([LocalStorage(library)], True, exclude=["_old"]),
                   enrich=False, backups_dir=project.backups_dir)
    res = run_alerts(project, settings, use_browser=False, http=LocalOnlyHttp(), printer=lambda *a: None)
    assert res["counts"]["downloaded"] == 2
    assert (library / "_new" / "PREP 25 Doe - Hierarchical_MA-DRL_for_E2E_NS_in_O-RAN.pdf").exists()
    assert (library / "_new" / "jcomnet 26 Kumar - Joint_admission_control_and_RA_for_NS_using_constrained_DRL.pdf").exists()
    flags = _flags(imap_server["port"])
    assert flags["<scholar-0001@google.com>"] and flags["<sd-0001@notification.elsevier.com>"]
    assert not flags["<personal-0001@ieee.org>"]                     # never opened, never marked
    res2 = run_alerts(project, settings, use_browser=False, http=LocalOnlyHttp(), printer=lambda *a: None)
    assert [d.status for d in res2["decisions"]] == ["needs_manual"]  # only the earlier paywalled retry
