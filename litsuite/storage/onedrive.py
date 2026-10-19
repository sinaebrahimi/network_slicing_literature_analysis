"""OneDrive (personal or work/school) through Microsoft Graph.

Settings::

    cloud:
      onedrive:
        client_id: "00000000-0000-0000-0000-000000000000"   # your Azure app (see README > OneDrive)
        tenant: "common"          # "consumers" for personal only, "organizations" or your tenant ID for work/school
    projects:
      myproject:
        library: "onedrive:/Documents/PhD/Literature"

Sign in once with ``litsuite login cloud onedrive``.
If the OneDrive desktop app is installed, ``library: "{onedrive}/Documents/PhD/Literature"``
is simpler and needs no app registration.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import quote

import requests

from litsuite.oauth import GRAPH, microsoft_token
from litsuite.storage.base import LibraryFile, Storage

SCOPES = ["Files.ReadWrite"]
CHUNK = 320 * 1024 * 10  # upload sessions need multiples of 320 KiB


def login(client_id: str, tenant: str = "common") -> str:
    microsoft_token(client_id, SCOPES, "onedrive", tenant, interactive=True)
    return "OneDrive connected."


class OneDriveStorage(Storage):
    def __init__(self, root: str, client_id: str = "", tenant: str = "common", session=None, token: str | None = None):
        self.root = root.strip("/")
        self.spec = f"onedrive:/{self.root}"
        self.http = session or requests.Session()
        if token is None:
            token = microsoft_token(client_id, SCOPES, "onedrive", tenant, interactive=False)
        self.http.headers.update({"Authorization": f"Bearer {token}"})

    def _item_url(self, relative: str = "") -> str:
        path = "/".join(p for p in (self.root, relative.strip("/")) if p)
        return f"{GRAPH}/me/drive/root" + (f":/{quote(path)}:" if path else "")

    def _get(self, url: str, **kw) -> dict:
        r = self.http.get(url, timeout=60, **kw)
        if r.status_code == 404:
            raise FileNotFoundError(url)
        r.raise_for_status()
        return r.json()

    def _walk(self) -> list:
        out, stack = [], [""]
        while stack:
            rel_dir = stack.pop()
            url = self._item_url(rel_dir) + "/children?$top=999&$select=name,size,folder,file,lastModifiedDateTime"
            while url:
                data = self._get(url)
                for item in data.get("value", []):
                    rel = f"{rel_dir}/{item['name']}" if rel_dir else item["name"]
                    if "folder" in item:
                        stack.append(rel)
                    elif "file" in item:
                        out.append(LibraryFile(rel, item.get("size"), item.get("lastModifiedDateTime")))
                url = data.get("@odata.nextLink")
        return out

    def save(self, local_path: Path, relative_path: str) -> str:
        data = Path(local_path).read_bytes()
        target = self._item_url(relative_path)
        if len(data) <= 4 * 1024 * 1024:
            r = self.http.put(target + "/content?@microsoft.graph.conflictBehavior=rename", data=data,
                              headers={"Content-Type": "application/pdf"}, timeout=120)
            r.raise_for_status()
        else:
            r = self.http.post(target + "/createUploadSession",
                               json={"item": {"@microsoft.graph.conflictBehavior": "rename"}}, timeout=60)
            r.raise_for_status()
            upload_url = r.json()["uploadUrl"]
            for start in range(0, len(data), CHUNK):
                chunk = data[start:start + CHUNK]
                end = start + len(chunk) - 1
                rr = requests.put(upload_url, data=chunk, timeout=300, headers={
                    "Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{len(data)}"})
                rr.raise_for_status()
        return f"onedrive:/{self.root}/{relative_path}"

    def rename(self, old_relative: str, new_relative: str) -> None:
        new_p = PurePosixPath(new_relative)
        body = {"name": new_p.name}
        if PurePosixPath(old_relative).parent != new_p.parent:
            parent_rel = str(new_p.parent) if str(new_p.parent) != "." else ""
            parent_path = "/".join(p for p in (self.root, parent_rel) if p)
            body["parentReference"] = {"path": "/drive/root:/" + parent_path}
        r = self.http.patch(self._item_url(old_relative), json=body, timeout=60)
        r.raise_for_status()
