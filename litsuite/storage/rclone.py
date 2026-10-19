"""Any cloud supported by rclone (https://rclone.org): Box, pCloud, MEGA, Nextcloud,
SharePoint, S3, WebDAV, and also Google Drive / OneDrive / Dropbox without
registering your own API app.

Settings:  ``library: "rclone:myremote:Research/Literature"``
Set up the remote once with ``rclone config`` (it handles the browser login).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from litsuite.storage.base import LibraryFile, Storage


class RcloneStorage(Storage):
    def __init__(self, remote_path: str, rclone_binary: str = "rclone", runner=subprocess.run):
        self.remote = remote_path.rstrip("/")
        self.spec = f"rclone:{self.remote}"
        self.binary = rclone_binary
        self._run = runner

    def _cmd(self, *args) -> str:
        if self._run is subprocess.run and not shutil.which(self.binary):
            raise RuntimeError("rclone is not installed. Get it from https://rclone.org/downloads/ "
                               "and run 'rclone config' to add your cloud account.")
        proc = self._run([self.binary, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"rclone {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _path(self, relative: str) -> str:
        return f"{self.remote}/{relative}" if relative else self.remote

    def _walk(self) -> list:
        out = self._cmd("lsjson", "-R", "--files-only", "--no-mimetype", self.remote)
        items = json.loads(out or "[]")
        return [LibraryFile(i["Path"], i.get("Size"), i.get("ModTime")) for i in items if not i.get("IsDir")]

    def save(self, local_path: Path, relative_path: str) -> str:
        self._cmd("copyto", str(local_path), self._path(relative_path))
        return self._path(relative_path)

    def rename(self, old_relative: str, new_relative: str) -> None:
        self._cmd("moveto", self._path(old_relative), self._path(new_relative))
