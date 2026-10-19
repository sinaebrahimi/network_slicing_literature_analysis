"""Passwords and tokens, kept out of settings.yaml and out of the repository.

Lookup order for a secret named e.g. ``imap:uni-mail``:
  1. environment variable ``LITSUITE_SECRET_IMAP_UNI_MAIL`` (handy for schedulers/CI)
  2. the operating system credential store via ``keyring``
     (Windows Credential Manager, macOS Keychain, Linux Secret Service)
  3. ``~/.litsuite/secrets.json`` (file readable only by you), used when no
     credential store is available

OAuth token files (Gmail, Outlook, Google Drive, OneDrive, Dropbox) are stored
in ``~/.litsuite/tokens/``; browser sessions in ``~/.litsuite/browser/``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

from litsuite.config import APP_HOME

log = logging.getLogger(__name__)
SERVICE = "litsuite"
SECRETS_FILE = APP_HOME / "secrets.json"
TOKENS_DIR = APP_HOME / "tokens"


def _env_name(key: str) -> str:
    return "LITSUITE_SECRET_" + re.sub(r"[^A-Za-z0-9]+", "_", key).upper().strip("_")


def _keyring():
    try:
        import keyring

        backend = keyring.get_keyring()
        if "fail" in type(backend).__name__.lower() or "null" in type(backend).__name__.lower():
            return None
        return keyring
    except Exception:
        return None


def _read_file() -> dict:
    try:
        return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(SECRETS_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_secret(key: str):
    env = os.environ.get(_env_name(key))
    if env:
        return env
    kr = _keyring()
    if kr:
        try:
            val = kr.get_password(SERVICE, key)
            if val:
                return val
        except Exception as e:  # locked keychain, no D-Bus session...
            log.debug("keyring read failed for %s: %s", key, e)
    return _read_file().get(key)


def set_secret(key: str, value: str) -> str:
    """Store a secret; returns where it was stored."""
    kr = _keyring()
    if kr:
        try:
            kr.set_password(SERVICE, key, value)
            return "system credential store"
        except Exception as e:
            log.debug("keyring write failed for %s: %s", key, e)
    data = _read_file()
    data[key] = value
    _write_file(data)
    return str(SECRETS_FILE)


def delete_secret(key: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, key)
        except Exception:
            pass
    data = _read_file()
    if data.pop(key, None) is not None:
        _write_file(data)


def token_path(kind: str, name: str) -> Path:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    return TOKENS_DIR / f"{kind}-{safe}.json"


def secure_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
