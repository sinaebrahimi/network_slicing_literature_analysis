"""Cloud backends against in-memory fakes of their APIs (no network)."""

import json
import types
from pathlib import Path

import pytest

from conftest import FAKE_PDF

from litsuite.storage import LocalStorage, folder_allowed, open_storage
from litsuite.storage.dropbox_backend import DropboxStorage
from litsuite.storage.gdrive import FOLDER, GoogleDriveStorage
from litsuite.storage.onedrive import OneDriveStorage
from litsuite.storage.rclone import RcloneStorage


def test_folder_rules():
    assert folder_allowed("", False)
    assert not folder_allowed("Surveys", False)
    assert folder_allowed("Surveys/2024", True)
    assert not folder_allowed("_old", True, exclude=["_old"])
    assert not folder_allowed("Surveys/_old", True, exclude=["_old"])
    assert folder_allowed("Surveys", True, include=["surveys"]) and not folder_allowed("Books", True, include=["Surveys"])


def test_local_storage(library, tmp_path):
    st = open_storage(str(library))
    assert isinstance(st, LocalStorage)
    names = [f.relative_path for f in st.list_files(recursive=True, exclude=["_old"])]
    assert "Surveys/COMST 22 Wu - A_Survey_on_NS_Enabling_Technologies.pdf" in names and len(names) == 4
    assert len(st.list_files(recursive=False)) == 3
    src = tmp_path / "new.pdf"
    src.write_bytes(FAKE_PDF)
    st.save(src, "_new/X 25 - Y.pdf")
    assert (library / "_new" / "X 25 - Y.pdf").exists()
    st.rename("_new/X 25 - Y.pdf", "X 25 - Y.pdf")
    assert st.exists("X 25 - Y.pdf") and not st.exists("_new/X 25 - Y.pdf")
    with pytest.raises(FileNotFoundError):
        LocalStorage(tmp_path / "missing").list_files()


def test_rclone_storage():
    calls = []

    def runner(cmd, capture_output, text):
        calls.append(cmd)
        out = ""
        if cmd[1] == "lsjson":
            out = json.dumps([{"Path": "A 24 - T.pdf", "Size": 10, "ModTime": "x", "IsDir": False},
                              {"Path": "Surveys/B 23 - S.pdf", "Size": 5, "IsDir": False}])
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    st = RcloneStorage("box:Research/Lit", runner=runner)
    assert [f.relative_path for f in st.list_files()] == ["A 24 - T.pdf", "Surveys/B 23 - S.pdf"]
    st.save(Path("/tmp/x.pdf"), "_new/C.pdf")
    st.rename("_new/C.pdf", "C.pdf")
    assert calls[-2] == ["rclone", "copyto", "/tmp/x.pdf", "box:Research/Lit/_new/C.pdf"]
    assert calls[-1] == ["rclone", "moveto", "box:Research/Lit/_new/C.pdf", "box:Research/Lit/C.pdf"]


class FakeDropbox:
    def __init__(self):
        self.uploads, self.moves = [], []

    def files_list_folder(self, path, recursive):
        assert path == "/Research/Lit" and recursive
        f = lambda p, s: types.SimpleNamespace(path_display=p, path_lower=p.lower(), size=s, server_modified="t")  # noqa
        return types.SimpleNamespace(entries=[f("/Research/Lit/A 24 - T.pdf", 1),
                                              types.SimpleNamespace(path_display="/Research/Lit/Surveys",
                                                                    path_lower="/research/lit/surveys")],
                                     has_more=True, cursor="c1")

    def files_list_folder_continue(self, cursor):
        f = types.SimpleNamespace(path_display="/Research/Lit/Surveys/B.pdf", path_lower="/research/lit/surveys/b.pdf",
                                  size=2, server_modified="t")
        return types.SimpleNamespace(entries=[f], has_more=False, cursor="c2")

    def files_upload(self, data, path, mode, autorename):
        self.uploads.append(path)

    def files_move_v2(self, a, b):
        self.moves.append((a, b))


def test_dropbox_storage(tmp_path):
    pytest.importorskip("dropbox")
    fake = FakeDropbox()
    st = DropboxStorage("/Research/Lit/", client=fake)
    assert [f.relative_path for f in st.list_files()] == ["A 24 - T.pdf", "Surveys/B.pdf"]
    src = tmp_path / "x.pdf"
    src.write_bytes(FAKE_PDF)
    st.save(src, "C 25 - New.pdf")
    st.rename("C 25 - New.pdf", "Surveys/C 25 - New.pdf")
    assert fake.uploads == ["/Research/Lit/C 25 - New.pdf"]
    assert fake.moves == [("/Research/Lit/C 25 - New.pdf", "/Research/Lit/Surveys/C 25 - New.pdf")]


class FakeDrive:
    """Tiny in-memory Google Drive: files().list/create/update(...).execute()."""

    def __init__(self):
        self.items = {"root": {"name": "My Drive", "mimeType": FOLDER, "parents": []}}
        self._n = 0
        for name, parent, mime in [("Research", "root", FOLDER), ("Lit", "f1", FOLDER), ("A 24 - T.pdf", "f2", "application/pdf"),
                                   ("Surveys", "f2", FOLDER), ("B.pdf", "f4", "application/pdf"),
                                   ("Doc", "f2", "application/vnd.google-apps.document")]:
            self._add(name, parent, mime)
        self.call = None

    def _add(self, name, parent, mime):
        self._n += 1
        fid = f"f{self._n}"
        self.items[fid] = {"name": name, "mimeType": mime, "parents": [parent], "size": "10"}
        return fid

    def files(self):
        return self

    def list(self, q, fields=None, pageSize=None, pageToken=None, **kw):
        parent = q.split("'")[1]
        only_folders = "mimeType = " in q
        name = q.split("name = '")[1].split("'")[0] if "name = '" in q else None
        res = [{"id": k, **v} for k, v in self.items.items() if parent in v["parents"]
               and (not only_folders or v["mimeType"] == FOLDER) and (name is None or v["name"] == name)]
        self.call = {"files": res}
        return self

    def create(self, body, fields=None, media_body=None, **kw):
        self.call = {"id": self._add(body["name"], body["parents"][0], body.get("mimeType", "application/pdf"))}
        return self

    def update(self, fileId, body, addParents=None, removeParents=None, **kw):
        self.items[fileId]["name"] = body["name"]
        if addParents:
            self.items[fileId]["parents"] = [addParents]
        self.call = {}
        return self

    def execute(self):
        return self.call


def test_gdrive_storage(tmp_path, monkeypatch):
    pytest.importorskip("googleapiclient")
    import googleapiclient.http as gh

    monkeypatch.setattr(gh, "MediaFileUpload", lambda *a, **k: None)
    drive = FakeDrive()
    st = GoogleDriveStorage("/Research/Lit", service=drive)
    assert sorted(f.relative_path for f in st.list_files()) == ["A 24 - T.pdf", "Surveys/B.pdf"]  # Google Doc skipped
    src = tmp_path / "x.pdf"
    src.write_bytes(FAKE_PDF)
    st.save(src, "_new/C.pdf")                       # creates the _new folder
    assert "_new/C.pdf" in [f.relative_path for f in st.list_files()]
    st.rename("_new/C.pdf", "C.pdf")
    assert "C.pdf" in [f.relative_path for f in st.list_files()]
    with pytest.raises(FileNotFoundError):
        GoogleDriveStorage("/Research/Nope", service=drive).list_files()


class FakeGraphSession:
    def __init__(self):
        self.headers, self.puts, self.patches = {}, [], []

    def get(self, url, timeout=60):
        if "Lit:/children" in url:
            data = {"value": [{"name": "A 24 - T.pdf", "file": {}, "size": 3},
                              {"name": "Surveys", "folder": {}}], "@odata.nextLink": "NEXT"}
        elif url == "NEXT":
            data = {"value": [{"name": "Z.pdf", "file": {}, "size": 1}]}
        elif "Surveys:/children" in url:
            data = {"value": [{"name": "B.pdf", "file": {}, "size": 2}]}
        else:
            raise AssertionError(url)
        return types.SimpleNamespace(status_code=200, json=lambda: data, raise_for_status=lambda: None)

    def put(self, url, data, headers, timeout):
        self.puts.append(url)
        return types.SimpleNamespace(raise_for_status=lambda: None)

    def patch(self, url, json, timeout):
        self.patches.append((url, json))
        return types.SimpleNamespace(raise_for_status=lambda: None)


def test_onedrive_storage(tmp_path):
    s = FakeGraphSession()
    st = OneDriveStorage("/Documents/Lit", session=s, token="tok")
    assert sorted(f.relative_path for f in st.list_files()) == ["A 24 - T.pdf", "Surveys/B.pdf", "Z.pdf"]
    assert s.headers["Authorization"] == "Bearer tok"
    src = tmp_path / "x.pdf"
    src.write_bytes(FAKE_PDF)
    st.save(src, "C 25 - New.pdf")
    assert s.puts[0].endswith("root:/Documents/Lit/C%2025%20-%20New.pdf:/content?@microsoft.graph.conflictBehavior=rename")
    st.rename("C 25 - New.pdf", "Surveys/C 25 - New.pdf")
    assert s.patches[0][1] == {"name": "C 25 - New.pdf", "parentReference": {"path": "/drive/root:/Documents/Lit/Surveys"}}


def test_open_storage_dispatch_errors(monkeypatch):
    from litsuite.oauth import AuthError

    with pytest.raises(AuthError):        # not signed in yet
        open_storage("dropbox:/x")
    with pytest.raises((AuthError, Exception)):
        open_storage("onedrive:/x")
    assert open_storage("rclone:r:p").spec == "rclone:r:p"


def test_sync_folder_detection(tmp_path, monkeypatch):
    from litsuite.storage import sync_folders

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "OneDrive - Coventry University").mkdir()
    (tmp_path / "Dropbox").mkdir()
    (tmp_path / "Library" / "CloudStorage" / "GoogleDrive-me@gmail.com" / "My Drive").mkdir(parents=True)
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    found = sync_folders.detect_all()
    assert found["onedrive"][0].name == "OneDrive - Coventry University"
    assert found["dropbox"][0].name == "Dropbox"
    assert found["gdrive"][0].name == "My Drive"
    vals = sync_folders.placeholder_values()
    assert vals["onedrive_business"].endswith("OneDrive - Coventry University")
