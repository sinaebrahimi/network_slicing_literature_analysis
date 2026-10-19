"""Google Drive through the Drive API.

Settings::

    cloud:
      gdrive:
        client_secret_file: "~/.litsuite/google_client_secret.json"   # see README > Google Drive
    projects:
      myproject:
        library: "gdrive:/Research/Literature"      # path from "My Drive"
        # or     "gdrive:id:1AbCdEf..."             # a folder ID (also works for shared drives)

Sign in once with ``litsuite login cloud gdrive``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from litsuite.oauth import AuthError, google_credentials
from litsuite.storage.base import LibraryFile, Storage

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER = "application/vnd.google-apps.folder"
COMMON = {"supportsAllDrives": True}


def login(client_secret_file: str) -> str:
    google_credentials(client_secret_file, SCOPES, "gdrive", interactive=True)
    return "Google Drive connected."


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveStorage(Storage):
    def __init__(self, target: str, client_secret_file: str = "", service=None):
        self.target = target
        self.spec = f"gdrive:{target}"
        if service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as e:
                raise AuthError("Google Drive support needs: pip install google-api-python-client "
                                "google-auth-oauthlib") from e
            creds = google_credentials(client_secret_file, SCOPES, "gdrive", interactive=False)
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.svc = service
        self._root_id = None

    # -- folder helpers --------------------------------------------------
    def _children(self, folder_id: str, only_folders: bool = False) -> list:
        q = f"'{folder_id}' in parents and trashed = false"
        if only_folders:
            q += f" and mimeType = '{FOLDER}'"
        items, token = [], None
        while True:
            resp = self.svc.files().list(q=q, pageSize=1000, pageToken=token,
                                         fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                                         includeItemsFromAllDrives=True, **COMMON).execute()
            items += resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return items

    def _find_child(self, parent: str, name: str, create: bool = False):
        for item in self._children(parent, only_folders=True):
            if item["name"] == name:
                return item["id"]
        if not create:
            return None
        meta = {"name": name, "mimeType": FOLDER, "parents": [parent]}
        return self.svc.files().create(body=meta, fields="id", **COMMON).execute()["id"]

    @property
    def root_id(self) -> str:
        if self._root_id:
            return self._root_id
        if self.target.startswith("id:"):
            self._root_id = self.target[3:]
            return self._root_id
        current = "root"
        for part in PurePosixPath("/" + self.target.strip("/")).parts[1:]:
            nxt = self._find_child(current, part)
            if not nxt:
                raise FileNotFoundError(f"Google Drive folder not found: {self.target} (missing '{part}')")
            current = nxt
        self._root_id = current
        return current

    def _folder_for(self, subfolder: str, create: bool) -> str:
        current = self.root_id
        for part in PurePosixPath(subfolder).parts if subfolder else ():
            nxt = self._find_child(current, part, create=create)
            if not nxt:
                raise FileNotFoundError(subfolder)
            current = nxt
        return current

    def _file_id(self, relative: str) -> str:
        p = PurePosixPath(relative)
        folder = self._folder_for(str(p.parent) if str(p.parent) != "." else "", create=False)
        resp = self.svc.files().list(q=f"'{folder}' in parents and name = '{_q(p.name)}' and trashed = false",
                                     fields="files(id)", includeItemsFromAllDrives=True, **COMMON).execute()
        files = resp.get("files", [])
        if not files:
            raise FileNotFoundError(relative)
        return files[0]["id"]

    # -- Storage API -----------------------------------------------------
    def _walk(self) -> list:
        out, stack = [], [(self.root_id, "")]
        while stack:
            folder_id, prefix = stack.pop()
            for item in self._children(folder_id):
                rel = f"{prefix}/{item['name']}" if prefix else item["name"]
                if item["mimeType"] == FOLDER:
                    stack.append((item["id"], rel))
                elif not item["mimeType"].startswith("application/vnd.google-apps"):
                    out.append(LibraryFile(rel, int(item.get("size", 0) or 0), item.get("modifiedTime")))
        return out

    def save(self, local_path: Path, relative_path: str) -> str:
        from googleapiclient.http import MediaFileUpload

        p = PurePosixPath(relative_path)
        parent = self._folder_for(str(p.parent) if str(p.parent) != "." else "", create=True)
        media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=True)
        self.svc.files().create(body={"name": p.name, "parents": [parent]}, media_body=media,
                                fields="id", **COMMON).execute()
        return f"gdrive:{self.target}/{relative_path}"

    def rename(self, old_relative: str, new_relative: str) -> None:
        fid = self._file_id(old_relative)
        old_p, new_p = PurePosixPath(old_relative), PurePosixPath(new_relative)
        kwargs = {}
        if old_p.parent != new_p.parent:
            kwargs["addParents"] = self._folder_for(str(new_p.parent) if str(new_p.parent) != "." else "", True)
            kwargs["removeParents"] = self._folder_for(str(old_p.parent) if str(old_p.parent) != "." else "", False)
        self.svc.files().update(fileId=fid, body={"name": new_p.name}, **kwargs, **COMMON).execute()
