"""Cloud-storage and mail back-ends driven through the REAL client libraries
(dropbox SDK, googleapiclient, msal, google-auth, requests, keyring). Only the
network is replaced, by small in-memory servers that speak the documented APIs."""

import base64
import json
import re
import threading
import time
import urllib.parse
import urllib.request

import pytest
import responses

from conftest import FAKE_PDF, FIXTURES

DBX = "https://api.dropboxapi.com"
DBX_CONTENT = "https://content.dropboxapi.com"


def _file_meta(path, size=10):
    return {".tag": "file", "name": path.rsplit("/", 1)[1], "id": "id:" + str(abs(hash(path)) % 10**9),
            "client_modified": "2026-10-01T10:00:00Z", "server_modified": "2026-10-01T10:00:00Z",
            "rev": "0123456789abcdef", "size": size, "path_lower": path.lower(), "path_display": path,
            "is_downloadable": True, "content_hash": "0" * 64}


# ---------------------------------------------------------------- Dropbox (official SDK)
@responses.activate
def test_dropbox_sdk_login_list_upload_move(tmp_path):
    pytest.importorskip("dropbox")
    from litsuite.secrets import get_secret
    from litsuite.storage import open_storage
    from litsuite.storage.dropbox_backend import login
    from litsuite.config import Settings

    tokens = []

    def token(request):
        body = urllib.parse.parse_qs(request.body)
        tokens.append(body["grant_type"][0])
        if body["grant_type"] == ["authorization_code"]:
            assert body["code"] == ["the-code"] and "code_verifier" in body      # PKCE, no app secret
            return 200, {}, json.dumps({"access_token": "a1", "refresh_token": "r1", "expires_in": 14400,
                                        "token_type": "bearer", "account_id": "dbid:x", "uid": "1"})
        assert body["refresh_token"] == ["r1"]
        return 200, {}, json.dumps({"access_token": "a2", "expires_in": 14400, "token_type": "bearer"})

    responses.add_callback(responses.POST, f"{DBX}/oauth2/token", callback=token, content_type="application/json")
    printed = []
    assert "connected" in login("appkey123", input_fn=lambda _="": "the-code", printer=printed.append)
    assert "https://www.dropbox.com/oauth2/authorize?" in printed[0] and "client_id=appkey123" in printed[0]
    assert get_secret("dropbox:refresh_token") == "r1"

    store = {"/Research/Lit/A 24 - T.pdf": 10, "/Research/Lit/Surveys/B 23 - S.pdf": 20}

    def list_folder(request):
        assert request.headers["Authorization"] == "Bearer a2"
        args = json.loads(request.body)
        assert args["path"] == "/Research/Lit" and args["recursive"] is True
        entries = [{".tag": "folder", "name": "Surveys", "id": "id:f", "path_lower": "/research/lit/surveys",
                    "path_display": "/Research/Lit/Surveys"}]
        entries += [_file_meta(p, s) for p, s in list(store.items())[:1]]
        return 200, {}, json.dumps({"entries": entries, "cursor": "c1", "has_more": True})

    def list_continue(request):
        assert json.loads(request.body) == {"cursor": "c1"}
        return 200, {}, json.dumps({"entries": [_file_meta(p, s) for p, s in list(store.items())[1:]],
                                    "cursor": "c2", "has_more": False})

    def upload(request):
        arg = json.loads(request.headers["Dropbox-API-Arg"])
        assert arg["mode"] in ("add", {".tag": "add"}) and arg["autorename"] is True
        assert request.body == FAKE_PDF
        store[arg["path"]] = len(request.body)
        return 200, {}, json.dumps(_file_meta(arg["path"], len(request.body)))

    def move(request):
        a = json.loads(request.body)
        store[a["to_path"]] = store.pop(a["from_path"])
        return 200, {}, json.dumps({"metadata": _file_meta(a["to_path"])})

    responses.add_callback(responses.POST, f"{DBX}/2/files/list_folder", callback=list_folder, content_type="application/json")
    responses.add_callback(responses.POST, f"{DBX}/2/files/list_folder/continue", callback=list_continue, content_type="application/json")
    responses.add_callback(responses.POST, f"{DBX_CONTENT}/2/files/upload", callback=upload, content_type="application/json")
    responses.add_callback(responses.POST, f"{DBX}/2/files/move_v2", callback=move, content_type="application/json")

    settings = Settings({"cloud": {"dropbox": {"app_key": "appkey123"}}})
    st = open_storage("dropbox:/Research/Lit", settings)
    assert [f.relative_path for f in st.list_files()] == ["A 24 - T.pdf", "Surveys/B 23 - S.pdf"]
    assert tokens == ["authorization_code", "refresh_token"]       # access token refreshed automatically
    src = tmp_path / "x.pdf"
    src.write_bytes(FAKE_PDF)
    assert st.save(src, "_new/C 25 - New.pdf") == "dropbox:/Research/Lit/_new/C 25 - New.pdf"
    st.rename("_new/C 25 - New.pdf", "C 25 - New.pdf")
    assert "/Research/Lit/C 25 - New.pdf" in store


# ---------------------------------------------------------------- Google (googleapiclient + static discovery)
class GoogleRouter:
    """A minimal httplib2-compatible object serving Gmail/Drive REST calls from memory."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, uri, method="GET", body=None, headers=None, redirections=5, connection_type=None):
        import httplib2

        u = urllib.parse.urlparse(uri)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.calls.append((method, u.path, q, body))
        status, data, extra = self.handler(method, u.path, q, body, headers or {})
        resp = httplib2.Response({"status": str(status), "content-type": "application/json", **extra})
        return resp, (json.dumps(data) if not isinstance(data, (bytes, str)) else data).encode() \
            if not isinstance(data, bytes) else data


def test_gmail_api_real_client():
    pytest.importorskip("googleapiclient")
    from googleapiclient.discovery import build

    from litsuite.mail.gmail import GmailMailbox

    raw = {"m1": (FIXTURES / "emails" / "scholar_alert.eml").read_bytes(),
           "m2": (FIXTURES / "emails" / "personal_ieee.eml").read_bytes()}
    modified = []

    def handler(method, path, q, body, headers):
        if path.endswith("/messages") and method == "GET":
            assert "is:unread" in q["q"] and "from:(scholaralerts-noreply@google.com)" in q["q"]
            if "pageToken" not in q:
                return 200, {"messages": [{"id": "m1", "threadId": "t"}], "nextPageToken": "p2"}, {}
            return 200, {"messages": [{"id": "m2", "threadId": "t"}]}, {}
        if path.endswith("/batchModify"):
            modified.append(json.loads(body))
            return 204, b"", {}
        mid = path.rsplit("/", 1)[1]
        assert q["format"] == "raw"
        return 200, {"id": mid, "raw": base64.urlsafe_b64encode(raw[mid]).decode()}, {}

    router = GoogleRouter(handler)
    svc = build("gmail", "v1", http=router, static_discovery=True)
    mb = GmailMailbox({"name": "g"}, service=svc)
    import datetime as dt

    msgs = list(mb.search(["scholaralerts-noreply@google.com"], dt.date(2026, 1, 1)))
    assert [m.handle for m in msgs] == ["m1"]                      # m2 is from another sender
    assert all(path.startswith("/gmail/v1/users/me/") for _, path, _, _ in router.calls)
    assert mb.mark_read(msgs) == 1 and modified == [{"ids": ["m1"], "removeLabelIds": ["UNREAD"]}]


def test_google_drive_real_client(tmp_path):
    pytest.importorskip("googleapiclient")
    from googleapiclient.discovery import build

    from litsuite.storage.gdrive import FOLDER, GoogleDriveStorage

    files = {"root": {"name": "My Drive", "mimeType": FOLDER, "parents": []},
             "f1": {"name": "Research", "mimeType": FOLDER, "parents": ["root"]},
             "f2": {"name": "Lit", "mimeType": FOLDER, "parents": ["f1"]},
             "f3": {"name": "A 24 - It's ok.pdf", "mimeType": "application/pdf", "parents": ["f2"], "size": "11"}}
    uploads = {}

    def handler(method, path, q, body, headers):
        if hasattr(body, "read"):
            body = body.read()
        if path == "/drive/v3/files" and method == "GET":
            assert q["supportsAllDrives"] == "true" and q["includeItemsFromAllDrives"] == "true"
            parent = re.search(r"'([^']+)' in parents", q["q"]).group(1)
            name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q["q"])
            name = name.group(1).replace("\\'", "'") if name else None
            res = [{"id": k, **v} for k, v in files.items() if parent in v["parents"] and "trashed" in q["q"]
                   and (name is None or v["name"] == name) and ("mimeType =" not in q["q"] or v["mimeType"] == FOLDER)]
            return 200, {"files": res}, {}
        if path == "/drive/v3/files" and method == "POST":           # create a folder
            meta = json.loads(body)
            fid = f"f{len(files) + 1}"
            files[fid] = {"name": meta["name"], "mimeType": meta["mimeType"], "parents": meta["parents"]}
            return 200, {"id": fid}, {}
        if path == "/upload/drive/v3/files" and q.get("uploadType") == "resumable":
            if method == "POST":
                uploads["meta"] = json.loads(body)
                return 200, {}, {"location": "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=u1"}
            fid = f"f{len(files) + 1}"
            m = uploads["meta"]
            files[fid] = {"name": m["name"], "mimeType": "application/pdf", "parents": m["parents"], "size": str(len(body))}
            uploads["bytes"] = body
            return 200, {"id": fid}, {}
        if path.startswith("/drive/v3/files/") and method == "PATCH":
            fid = path.rsplit("/", 1)[1]
            files[fid]["name"] = json.loads(body)["name"]
            if q.get("addParents"):
                files[fid]["parents"] = [q["addParents"]]
            return 200, {"id": fid}, {}
        raise AssertionError((method, path, q))

    router = GoogleRouter(handler)
    svc = build("drive", "v3", http=router, static_discovery=True)
    st = GoogleDriveStorage("/Research/Lit", service=svc)
    assert [f.relative_path for f in st.list_files()] == ["A 24 - It's ok.pdf"]
    assert st.exists("A 24 - It's ok.pdf")
    src = tmp_path / "x.pdf"
    src.write_bytes(FAKE_PDF)
    st.save(src, "_new/B 25 - New.pdf")                          # creates _new, then resumable upload
    assert uploads["bytes"] == FAKE_PDF and uploads["meta"]["name"] == "B 25 - New.pdf"
    assert sorted(f.relative_path for f in st.list_files()) == ["A 24 - It's ok.pdf", "_new/B 25 - New.pdf"]
    st.rename("_new/B 25 - New.pdf", "B 25 - New.pdf")
    assert sorted(f.relative_path for f in st.list_files()) == ["A 24 - It's ok.pdf", "B 25 - New.pdf"]
    st.rename("A 24 - It's ok.pdf", "A 24 - Renamed.pdf")            # quote in a Drive query is escaped
    assert "A 24 - Renamed.pdf" in [f.relative_path for f in st.list_files()]


# ---------------------------------------------------------------- Microsoft identity (real MSAL)
def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(claims)}.sig"


def _mock_entra(tenant="organizations"):
    base = f"https://login.microsoftonline.com/{tenant}"
    responses.add(responses.GET, f"{base}/v2.0/.well-known/openid-configuration", json={
        "authorization_endpoint": f"{base}/oauth2/v2.0/authorize", "token_endpoint": f"{base}/oauth2/v2.0/token",
        "device_authorization_endpoint": f"{base}/oauth2/v2.0/devicecode",
        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0"})
    responses.add(responses.GET, re.compile(r"https://login\.microsoftonline\.com/common/discovery/instance.*"),
                  json={"tenant_discovery_endpoint": f"{base}/v2.0/.well-known/openid-configuration",
                        "metadata": [{"preferred_network": "login.microsoftonline.com",
                                      "aliases": ["login.microsoftonline.com"]}]})
    responses.add(responses.POST, f"{base}/oauth2/v2.0/devicecode", json={
        "device_code": "DC", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
        "expires_in": 900, "interval": 1,
        "message": "To sign in, use a web browser to open https://microsoft.com/devicelogin and enter the code ABCD-EFGH"})
    client_info = base64.urlsafe_b64encode(json.dumps({"uid": "u1", "utid": "t1"}).encode()).decode()
    grants = []

    def token(request):
        body = urllib.parse.parse_qs(request.body)
        grants.append(body["grant_type"][0])
        now = int(time.time())
        return 200, {}, json.dumps({
            "token_type": "Bearer", "scope": " ".join(body.get("scope", [""])[0].split()), "expires_in": 3600,
            "access_token": f"AT-{len(grants)}", "refresh_token": "RT", "client_info": client_info,
            "id_token": _jwt({"iss": "https://login.microsoftonline.com/t1/v2.0", "sub": "s", "aud": "cid",
                              "oid": "u1", "tid": "t1", "preferred_username": "me@uni.ac.uk",
                              "iat": now, "exp": now + 3600, "nbf": now})})

    responses.add_callback(responses.POST, f"{base}/oauth2/v2.0/token", callback=token, content_type="application/json")
    return grants


@responses.activate
def test_msal_device_flow_and_silent_reuse(monkeypatch):
    pytest.importorskip("msal")
    from litsuite.oauth import AuthError, microsoft_token
    from litsuite.secrets import token_path

    grants = _mock_entra("organizations")
    printed = []
    with pytest.raises(AuthError):                      # scheduled runs never start an interactive sign-in
        microsoft_token("cid", ["Mail.Read"], "mail-uni", "organizations", interactive=False)
    tok = microsoft_token("cid", ["Mail.Read"], "mail-uni", "organizations", printer=printed.append)
    assert tok == "AT-1" and "ABCD-EFGH" in printed[0] and grants == ["urn:ietf:params:oauth:grant-type:device_code"]
    assert token_path("msal", "mail-uni").exists()
    # A new process: the cached sign-in is reused silently (no prompt, no new device code).
    tok2 = microsoft_token("cid", ["Mail.Read"], "mail-uni", "organizations", interactive=False)
    assert tok2.startswith("AT-") and grants.count("urn:ietf:params:oauth:grant-type:device_code") == 1
    assert set(grants[1:]) <= {"refresh_token"} and len(printed) == 1
    with pytest.raises(AuthError):
        microsoft_token("", ["Mail.Read"], "x")


@responses.activate
def test_outlook_and_onedrive_with_real_msal_and_graph(tmp_path):
    pytest.importorskip("msal")
    import datetime as dt

    from litsuite.mail import outlook
    from litsuite.mail.outlook import OutlookMailbox
    from litsuite.storage import onedrive
    from litsuite.storage.onedrive import OneDriveStorage

    _mock_entra("organizations")
    cfg = {"name": "uni", "client_id": "cid", "tenant": "organizations", "mark_as_read": True}
    assert "connected" in outlook.login(cfg)
    html = (FIXTURES / "emails" / "ieee_alert.eml").read_text().split("\n\n", 1)[1]
    pages = {
        "first": {"value": [{"id": "A1", "subject": "IEEE Xplore Saved Search Alert", "isRead": False,
                             "from": {"emailAddress": {"address": "onlinealerts@ieee.org"}},
                             "receivedDateTime": "2026-10-30T10:00:00Z", "internetMessageId": "<a1@ieee.org>",
                             "body": {"contentType": "html", "content": html}}],
                  "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=2"},
        "second": {"value": [{"id": "A2", "subject": "lunch?", "isRead": False,
                              "from": {"emailAddress": {"address": "friend@uni.ac.uk"}},
                              "receivedDateTime": "2026-10-30T11:00:00Z", "body": {"contentType": "text", "content": "hi"}}]},
    }

    def messages(request):
        assert request.headers["Authorization"].startswith("Bearer AT-")
        if "skiptoken" in request.url:
            return 200, {}, json.dumps(pages["second"])
        assert "isRead eq false" in urllib.parse.unquote(request.url)
        return 200, {}, json.dumps(pages["first"])

    patched = []
    responses.add_callback(responses.GET, re.compile(r"https://graph\.microsoft\.com/v1\.0/me/messages.*"), callback=messages)
    responses.add_callback(responses.PATCH, re.compile(r"https://graph\.microsoft\.com/v1\.0/me/messages/.*"),
                           callback=lambda r: (patched.append((r.url, json.loads(r.body))) or (200, {}, "{}")))
    mb = OutlookMailbox(cfg)                                    # token from the cache, Mail.ReadWrite scope
    msgs = list(mb.search(["@ieee.org"], dt.date(2026, 10, 1)))
    assert [m.handle for m in msgs] == ["A1"]
    assert mb.mark_read(msgs) == 1 and patched[0][1] == {"isRead": True} and patched[0][0].endswith("/messages/A1")

    # OneDrive: listing with paging, small upload, large chunked upload session, move.
    assert "connected" in onedrive.login("cid", "organizations")
    drive = {"Documents/Lit/A 24 - T.pdf": 3, "Documents/Lit/Surveys/B.pdf": 5}
    chunks = []

    def children(request):
        path = urllib.parse.unquote(request.url.split("root:/", 1)[1].split(":/children")[0])
        names = {}
        for p, size in drive.items():
            if p.startswith(path + "/"):
                head = p[len(path) + 1:].split("/", 1)
                names[head[0]] = {"name": head[0], "folder": {"childCount": 1}} if len(head) > 1 \
                    else {"name": head[0], "file": {"mimeType": "application/pdf"}, "size": size}
        return 200, {}, json.dumps({"value": list(names.values())})

    def put_content(request):
        path = urllib.parse.unquote(request.url.split("root:/", 1)[1].split(":/content")[0])
        drive[path] = len(request.body)
        return 201, {}, json.dumps({"id": "new"})

    def create_session(request):
        path = urllib.parse.unquote(request.url.split("root:/", 1)[1].split(":/createUploadSession")[0])
        chunks.append(("session", path))
        return 200, {}, json.dumps({"uploadUrl": "https://upload.example.sharepoint.com/sess/1"})

    def upload_chunk(request):
        assert "Authorization" not in request.headers             # upload URLs must not get the token
        rng = request.headers["Content-Range"]
        chunks.append(rng)
        start, end_total = rng.split(" ")[1].split("-")
        end, total = end_total.split("/")
        if int(end) + 1 == int(total):
            drive[chunks[0][1]] = int(total)
            return 201, {}, json.dumps({"id": "big"})
        return 202, {}, json.dumps({"nextExpectedRanges": [f"{int(end) + 1}-"]})

    def move(request):
        body = json.loads(request.body)
        old = urllib.parse.unquote(request.url.split("root:/", 1)[1].rstrip(":"))
        parent = body.get("parentReference", {}).get("path", "/drive/root:/" + old.rsplit("/", 1)[0]).split("root:/", 1)[1]
        drive[f"{parent}/{body['name']}"] = drive.pop(old)
        return 200, {}, json.dumps({"id": "x"})

    G = r"https://graph\.microsoft\.com/v1\.0/me/drive/root:/"
    responses.add_callback(responses.GET, re.compile(G + r".*:/children.*"), callback=children)
    responses.add_callback(responses.PUT, re.compile(G + r".*:/content.*"), callback=put_content)
    responses.add_callback(responses.POST, re.compile(G + r".*:/createUploadSession"), callback=create_session)
    responses.add_callback(responses.PUT, "https://upload.example.sharepoint.com/sess/1", callback=upload_chunk)
    responses.add_callback(responses.PATCH, re.compile(G + r".*"), callback=move, content_type="application/json")
    st = OneDriveStorage("/Documents/Lit", "cid", "organizations")
    assert sorted(f.relative_path for f in st.list_files()) == ["A 24 - T.pdf", "Surveys/B.pdf"]
    small = tmp_path / "s.pdf"
    small.write_bytes(FAKE_PDF)
    st.save(small, "C 25 - Small.pdf")
    big = tmp_path / "b.pdf"
    big.write_bytes(b"%PDF" + b"0" * (9 * 1024 * 1024))           # 9 MB -> resumable upload in chunks
    st.save(big, "Surveys/D 25 - Big.pdf")
    assert drive["Documents/Lit/C 25 - Small.pdf"] == len(FAKE_PDF)
    assert drive["Documents/Lit/Surveys/D 25 - Big.pdf"] == big.stat().st_size
    assert len([c for c in chunks if c != chunks[0]]) == 3        # 3.125 MiB chunks, multiples of 320 KiB
    st.rename("C 25 - Small.pdf", "Surveys/C 25 - Small.pdf")
    assert "Documents/Lit/Surveys/C 25 - Small.pdf" in drive


# ---------------------------------------------------------------- Google OAuth (real google-auth / oauthlib)
@responses.activate
def test_google_oauth_consent_and_refresh(tmp_path, monkeypatch):
    pytest.importorskip("google_auth_oauthlib")
    import webbrowser

    from litsuite.oauth import AuthError, google_credentials
    from litsuite.secrets import token_path

    secret = tmp_path / "client.json"
    secret.write_text(json.dumps({"installed": {
        "client_id": "cid.apps.googleusercontent.com", "client_secret": "s", "project_id": "p",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"]}}))
    responses.add_passthru("http://localhost")
    grants = []

    def token(request):
        raw = request.body.decode() if isinstance(request.body, bytes) else request.body
        body = urllib.parse.parse_qs(raw)
        grants.append(body["grant_type"][0])
        return 200, {}, json.dumps({"access_token": f"g{len(grants)}", "expires_in": 3600, "refresh_token": "grt",
                                    "scope": "https://www.googleapis.com/auth/gmail.readonly", "token_type": "Bearer"})

    responses.add_callback(responses.POST, "https://oauth2.googleapis.com/token", callback=token, content_type="application/json")

    def fake_browser(url, *a, **k):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
        redirect = q["redirect_uri"][0]
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{redirect}?code=AUTH&state={q['state'][0]}&scope={q['scope'][0]}", timeout=10).read(), daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", fake_browser)
    monkeypatch.setattr(webbrowser, "get", lambda *a, **k: type("B", (), {"open": staticmethod(fake_browser)})())
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    with pytest.raises(AuthError):
        google_credentials(str(secret), scopes, "gmail-g", interactive=False)
    creds = google_credentials(str(secret), scopes, "gmail-g")
    assert creds.token == "g1" and grants == ["authorization_code"]
    saved = json.loads(token_path("google", "gmail-g").read_text())
    assert saved["refresh_token"] == "grt"
    # Expired access token: silently refreshed with the stored refresh token.
    saved["expiry"] = "2020-01-01T00:00:00Z"
    token_path("google", "gmail-g").write_text(json.dumps(saved))
    creds = google_credentials(str(secret), scopes, "gmail-g", interactive=False)
    assert creds.token == "g2" and grants == ["authorization_code", "refresh_token"]
    with pytest.raises(AuthError):
        google_credentials(str(tmp_path / "missing.json"), scopes, "other", interactive=True)


# ---------------------------------------------------------------- secrets with a real keyring backend
def test_secrets_real_keyring_env_and_file(tmp_path, monkeypatch):
    import os
    import stat

    import keyring
    from keyrings.alt.file import PlaintextKeyring

    import litsuite.secrets as sec

    kr = PlaintextKeyring()
    kr.file_path = str(tmp_path / "kr.cfg")
    keyring.set_keyring(kr)
    monkeypatch.setattr(sec, "_keyring", lambda: keyring)
    assert sec.set_secret("imap:uni", "pw1") == "system credential store"
    assert sec.get_secret("imap:uni") == "pw1" and keyring.get_password("litsuite", "imap:uni") == "pw1"
    monkeypatch.setenv("LITSUITE_SECRET_IMAP_UNI", "from-env")        # env wins (schedulers / CI)
    assert sec.get_secret("imap:uni") == "from-env"
    monkeypatch.delenv("LITSUITE_SECRET_IMAP_UNI")
    sec.delete_secret("imap:uni")
    assert sec.get_secret("imap:uni") is None
    # No credential store: private file fallback.
    monkeypatch.setattr(sec, "_keyring", lambda: None)
    where = sec.set_secret("sso:password", "pw2")
    assert where.endswith("secrets.json") and sec.get_secret("sso:password") == "pw2"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(sec.SECRETS_FILE).st_mode) == 0o600
    sec.delete_secret("sso:password")
    assert sec.get_secret("sso:password") is None
