"""Find the local folders of desktop cloud-sync clients (OneDrive, Google Drive, Dropbox...).

If the OneDrive / Google Drive / Dropbox / iCloud / Box desktop app is
installed, your cloud papers are already a normal folder on disk and litsuite
can use them directly (fastest, no API keys). These helpers find those folders
on Windows, macOS and Linux so settings can say ``{onedrive}/PhD/Literature``
and work on every computer you use.
"""

from __future__ import annotations

import json
import os
import string
import sys
from pathlib import Path


def _existing(paths) -> list:
    seen, out = set(), []
    for p in paths:
        if not p:
            continue
        p = Path(p).expanduser()
        try:
            if p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                out.append(p)
        except OSError:
            continue
    return out


def _cloudstorage(prefix: str) -> list:
    root = Path.home() / "Library" / "CloudStorage"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.name.startswith(prefix))


def onedrive_folders(kind: str = "any") -> list:
    """kind: 'personal', 'business' or 'any'."""
    personal = [os.environ.get("OneDriveConsumer")]
    business = [os.environ.get("OneDriveCommercial")]
    generic = [os.environ.get("OneDrive")]
    for p in _cloudstorage("OneDrive"):
        (business if "-" in p.name.replace("OneDrive-Personal", "") else personal).append(p)
    home = Path.home()
    for p in sorted(home.glob("OneDrive*")):
        (business if " - " in p.name else personal).append(p)
    if kind == "personal":
        return _existing(personal + generic)
    if kind == "business":
        return _existing(business)
    return _existing(business + personal + generic)


def gdrive_folders() -> list:
    cands = []
    for p in _cloudstorage("GoogleDrive"):
        cands += [p / "My Drive", p / "Shared drives", p]
    home = Path.home()
    cands += [home / "Google Drive" / "My Drive", home / "Google Drive", home / "My Drive"]
    if sys.platform.startswith("win"):
        for letter in string.ascii_uppercase[3:]:
            cands += [Path(f"{letter}:/My Drive")]
    return _existing(cands)


def dropbox_folders() -> list:
    cands = []
    info_files = [
        Path(os.environ.get("APPDATA", "")) / "Dropbox" / "info.json",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Dropbox" / "info.json",
        Path.home() / ".dropbox" / "info.json",
    ]
    for info in info_files:
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
            for acct in data.values():
                if isinstance(acct, dict) and acct.get("path"):
                    cands.append(acct["path"])
        except (OSError, ValueError):
            continue
    cands += _cloudstorage("Dropbox") + [Path.home() / "Dropbox"]
    return _existing(cands)


def icloud_folders() -> list:
    return _existing([
        Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
        Path.home() / "iCloudDrive",
        Path(os.environ.get("USERPROFILE", "")) / "iCloudDrive" if os.environ.get("USERPROFILE") else None,
    ])


def box_folders() -> list:
    return _existing(_cloudstorage("Box") + [Path.home() / "Box", Path.home() / "Box Sync"])


def detect_all() -> dict:
    """{label: [folders]} for every sync client found on this computer."""
    found = {
        "onedrive": onedrive_folders(),
        "gdrive": gdrive_folders(),
        "dropbox": dropbox_folders(),
        "icloud": icloud_folders(),
        "box": box_folders(),
    }
    return {k: v for k, v in found.items() if v}


def placeholder_values() -> dict:
    """Values for {onedrive}, {gdrive}, ... placeholders in settings paths."""
    values = {"home": str(Path.home())}
    for key, finder in (("onedrive", onedrive_folders), ("gdrive", gdrive_folders),
                        ("dropbox", dropbox_folders), ("icloud", icloud_folders),
                        ("box", box_folders)):
        found = finder()
        if found:
            values[key] = str(found[0])
    biz, per = onedrive_folders("business"), onedrive_folders("personal")
    if biz:
        values["onedrive_business"] = str(biz[0])
    if per:
        values["onedrive_personal"] = str(per[0])
    return values
