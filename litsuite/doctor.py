"""``litsuite doctor``: what is installed, configured and reachable."""

from __future__ import annotations

import importlib.util
import shutil
import sys

from litsuite.config import APP_HOME

OPTIONAL = [
    ("keyring", "keyring", "store passwords in the OS credential store"),
    ("playwright", "playwright", "institutional (paywalled) downloads through a browser"),
    ("googleapiclient", "google-api-python-client google-auth-oauthlib", "Gmail API / Google Drive"),
    ("msal", "msal", "Outlook / Microsoft 365 mail, OneDrive"),
    ("dropbox", "dropbox", "Dropbox API"),
    ("pandas", "pandas matplotlib seaborn wordcloud", "the dashboard notebook"),
]

ENDPOINTS = [
    ("CrossRef", "https://api.crossref.org/works?rows=1"),
    ("Semantic Scholar", "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1"),
    ("OpenAlex", "https://api.openalex.org/works?per-page=1"),
    ("Unpaywall", "https://api.unpaywall.org/v2/10.1038/nature12373?email=test@example.com"),
    ("arXiv", "https://export.arxiv.org/api/query?id_list=1706.03762"),
    ("doi.org", "https://doi.org/10.1109/ACCESS.2018.2881964"),
]


def run(settings, online: bool = False, printer=print) -> int:
    problems = 0
    printer(f"Python {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 9):
        printer("  ! Python 3.9 or newer is required")
        problems += 1
    printer(f"litsuite home (tokens, browser profile): {APP_HOME}")
    printer(f"settings file: {settings.path or 'NOT FOUND (run: litsuite init)'}")
    printer("\nOptional components:")
    for module, pip_name, purpose in OPTIONAL:
        ok = importlib.util.find_spec(module) is not None
        printer(f"  [{'x' if ok else ' '}] {module:16s} {purpose}" + ("" if ok else f"   -> pip install {pip_name}"))
    if importlib.util.find_spec("playwright"):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                path = p.chromium.executable_path
            import os

            printer(f"  [{'x' if os.path.exists(path) else ' '}] chromium for playwright"
                    + ("" if os.path.exists(path) else "   -> python -m playwright install chromium"))
        except Exception as e:
            printer(f"  [ ] chromium for playwright ({e})")
    printer(f"  [{'x' if shutil.which('rclone') else ' '}] rclone           other clouds (Box, pCloud, S3, ...)")

    printer("\nSettings check:")
    warnings = settings.validate()
    for w in warnings:
        printer("  ! " + w)
    if not warnings:
        printer("  ok")
    from litsuite.config import ConfigError

    for name in settings.project_names:
        prj = settings.project(name)
        try:
            specs = prj.library_specs
        except ConfigError:
            problems += 1   # already explained in the settings check above
            continue
        for spec in specs:
            if spec.split(":", 1)[0] in ("dropbox", "gdrive", "onedrive", "rclone"):
                printer(f"  [{name}] library {spec} (cloud API; test with: litsuite catalog update -p {name} --offline)")
                continue
            from pathlib import Path

            exists = Path(spec).is_dir()
            printer(f"  [{name}] library {spec}: {'found' if exists else 'NOT FOUND'}")
            problems += 0 if exists else 1

    if online:
        from litsuite.http import Http

        printer("\nScholarly services:")
        http = Http(settings.user_email)
        for label, url in ENDPOINTS:
            r = http.get(url, timeout=15, retries=0)
            ok = r is not None and r.status_code < 500
            printer(f"  [{'x' if ok else ' '}] {label:17s} {r.status_code if r is not None else 'unreachable'}")
            problems += 0 if ok else 1
        if not settings.user_email:
            printer("  ! set user.email in settings.yaml (CrossRef etiquette; required by Unpaywall)")
    printer("\n" + ("All good." if not problems else f"{problems} problem(s) found."))
    return 0 if not problems else 1
