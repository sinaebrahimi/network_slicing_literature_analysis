"""A folder on this computer (also covers OneDrive/Google Drive/Dropbox sync folders)."""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

from litsuite.storage.base import LibraryFile, Storage


class LocalStorage(Storage):
    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.spec = str(self.root)

    def _walk(self) -> list:
        if not self.root.is_dir():
            raise FileNotFoundError(f"Library folder not found: {self.root}")
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in filenames:
                full = Path(dirpath) / fn
                try:
                    st = full.stat()
                    size, mtime = st.st_size, dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                except OSError:  # e.g. cloud placeholder that is not downloaded
                    size, mtime = None, None
                out.append(LibraryFile(full.relative_to(self.root).as_posix(), size, mtime))
        return out

    def save(self, local_path: Path, relative_path: str) -> str:
        dest = self.root / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return str(dest)

    def rename(self, old_relative: str, new_relative: str) -> None:
        src, dst = self.root / old_relative, self.root / new_relative
        if dst.exists() and dst.resolve() != src.resolve():
            raise FileExistsError(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def exists(self, relative_path: str) -> bool:
        return (self.root / relative_path).exists()
