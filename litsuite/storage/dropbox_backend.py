"""Dropbox through its official API.

Settings::

    cloud:
      dropbox:
        app_key: "abc123xyz"        # from https://www.dropbox.com/developers/apps (see README)
    projects:
      myproject:
        library: "dropbox:/Research/Literature"

Sign in once with ``litsuite login cloud dropbox``.
"""

from __future__ import annotations

from pathlib import Path

from litsuite.oauth import AuthError
from litsuite.secrets import get_secret, set_secret
from litsuite.storage.base import LibraryFile, Storage

SECRET_KEY = "dropbox:refresh_token"


def login(app_key: str, input_fn=input, printer=print) -> str:
    try:
        import dropbox
    except ImportError as e:
        raise AuthError("Dropbox support needs: pip install dropbox") from e
    if not app_key:
        raise AuthError("Set cloud.dropbox.app_key in settings.yaml first (see README > Dropbox).")
    flow = dropbox.DropboxOAuth2FlowNoRedirect(app_key, use_pkce=True, token_access_type="offline")
    printer("1. Open this link and click 'Allow':\n   " + flow.start())
    code = input_fn("2. Paste the authorization code here: ").strip()
    result = flow.finish(code)
    where = set_secret(SECRET_KEY, result.refresh_token)
    return f"Dropbox connected (token saved in {where})"


class DropboxStorage(Storage):
    def __init__(self, root: str, app_key: str = "", client=None):
        self.root = "/" + root.strip("/") if root.strip("/") else ""
        self.spec = f"dropbox:{self.root or '/'}"
        if client is None:
            try:
                import dropbox
            except ImportError as e:
                raise AuthError("Dropbox support needs: pip install dropbox") from e
            token = get_secret(SECRET_KEY)
            if not token or not app_key:
                raise AuthError("Dropbox is not connected. Run: litsuite login cloud dropbox")
            client = dropbox.Dropbox(oauth2_refresh_token=token, app_key=app_key)
        self.dbx = client

    def _abs(self, relative: str) -> str:
        return f"{self.root}/{relative}".replace("//", "/")

    def _walk(self) -> list:
        res = self.dbx.files_list_folder(self.root, recursive=True)
        entries = list(res.entries)
        while res.has_more:
            res = self.dbx.files_list_folder_continue(res.cursor)
            entries += res.entries
        out, prefix = [], (self.root.lower() + "/") if self.root else "/"
        for e in entries:
            if not hasattr(e, "size"):  # folders and deleted entries have no size
                continue
            rel = e.path_display[len(prefix):] if e.path_lower.startswith(prefix) else e.path_display.lstrip("/")
            out.append(LibraryFile(rel, e.size, str(getattr(e, "server_modified", "") or "")))
        return out

    def save(self, local_path: Path, relative_path: str) -> str:
        import dropbox

        with open(local_path, "rb") as f:
            self.dbx.files_upload(f.read(), self._abs(relative_path), mode=dropbox.files.WriteMode.add,
                                  autorename=True)
        return "dropbox:" + self._abs(relative_path)

    def rename(self, old_relative: str, new_relative: str) -> None:
        self.dbx.files_move_v2(self._abs(old_relative), self._abs(new_relative))
