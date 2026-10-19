"""Common interface for every place a library can live (local disk or cloud)."""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass
class LibraryFile:
    relative_path: str          # POSIX style, relative to the library root
    size: int | None = None
    modified: str | None = None
    library: str = ""           # which library spec it came from

    @property
    def name(self) -> str:
        return PurePosixPath(self.relative_path).name

    @property
    def subfolder(self) -> str:
        parent = str(PurePosixPath(self.relative_path).parent)
        return "" if parent == "." else parent


def folder_allowed(subfolder: str, recursive: bool, include=(), exclude=()) -> bool:
    """Apply the recursive / include_folders / exclude_folders rules to a subfolder path."""
    if not subfolder:
        return True
    if not recursive:
        return False
    parts = PurePosixPath(subfolder).parts
    for pat in exclude or ():
        if any(fnmatch.fnmatch(p.lower(), str(pat).lower()) for p in parts) \
                or fnmatch.fnmatch(subfolder.lower(), str(pat).lower()):
            return False
    if include:
        return any(fnmatch.fnmatch(parts[0].lower(), str(pat).lower())
                   or fnmatch.fnmatch(subfolder.lower(), str(pat).lower()) for pat in include)
    return True


class Storage(ABC):
    spec: str = ""

    @abstractmethod
    def _walk(self) -> list:
        """Every PDF under the root (recursively) as LibraryFile objects."""

    @abstractmethod
    def save(self, local_path: Path, relative_path: str) -> str:
        """Copy/upload a local file to ``relative_path``; returns where it went."""

    @abstractmethod
    def rename(self, old_relative: str, new_relative: str) -> None:
        ...

    def describe(self) -> str:
        return self.spec

    def list_files(self, recursive: bool = True, include=(), exclude=(), extensions=(".pdf",)) -> list:
        exts = tuple(e.lower() for e in extensions)
        out = []
        for f in self._walk():
            if not f.name.lower().endswith(exts) or f.name.startswith((".", "~$")):
                continue
            if folder_allowed(f.subfolder, recursive, include, exclude):
                f.library = self.spec
                out.append(f)
        return sorted(out, key=lambda f: f.relative_path.lower())

    def subfolder_summary(self, extensions=(".pdf",)) -> Counter:
        """{top-level subfolder: number of PDFs inside it (at any depth)}."""
        counts: Counter = Counter()
        for f in self._walk():
            if f.name.lower().endswith(extensions) and f.subfolder:
                counts[PurePosixPath(f.subfolder).parts[0]] += 1
        return counts

    def exists(self, relative_path: str) -> bool:
        target = relative_path.lower()
        return any(f.relative_path.lower() == target for f in self._walk())
