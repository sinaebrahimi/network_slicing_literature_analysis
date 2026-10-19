"""``litsuite init``: answer a few questions, get a working settings.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from litsuite.config import is_remote_spec


def _ask(prompt, default="", input_fn=input):
    ans = input_fn(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return ans or default


def _list(text: str) -> list:
    return [t.strip() for t in re.split(r"[,;]", text or "") if t.strip()]


def project_block(name, library, recursive, focus, secondary, exclude, abbreviations) -> dict:
    return {name: {
        "description": "",
        "library": library,
        "recursive": recursive,
        "exclude_folders": [],
        "data_dir": f"projects/{name}",
        "catalog": "litreview.csv",
        "abbreviations": abbreviations,
        "keywords": {"focus": focus, "secondary": secondary, "exclude": exclude, "use_learned": True},
        "alerts": {"accounts": [], "mode": "ask", "since_days": 14, "download_folder": ""},
    }}


def run(settings, input_fn=input, printer=print, target=None) -> int:
    printer("litsuite setup: a project = one research area with its own PDF folder, catalog and keywords.\n")
    name = re.sub(r"\W+", "_", _ask("Project name (e.g. network_slicing, protein_folding)", "my_research",
                                    input_fn)).strip("_").lower() or "my_research"
    from litsuite.storage.sync_folders import detect_all

    found = detect_all()
    if found:
        printer("Cloud sync folders on this computer:")
        for k, v in found.items():
            printer(f"  {{{k}}} = {v[0]}")
        printer("You can write e.g. {onedrive}/Papers so the same settings work on all your computers.")
    printer("Cloud APIs are also possible: dropbox:/path, gdrive:/path, onedrive:/path, rclone:remote:path")
    library = _ask("Folder with your PDFs", "", input_fn).strip('"').strip("'")
    recursive = "ask"
    if library and not is_remote_spec(library) and "{" not in library:
        p = Path(library).expanduser()
        if p.is_dir():
            subs = sorted(d.name for d in p.iterdir() if d.is_dir() and any(d.rglob("*.pdf")))
            n_top = len(list(p.glob("*.pdf")))
            printer(f"  {n_top} PDFs at the top level" + (f"; subfolders with PDFs: {', '.join(subs)}" if subs else ""))
            if subs:
                recursive = _ask("Include subfolders? (yes/no/ask)", "yes", input_fn).lower()
                recursive = True if recursive.startswith("y") else False if recursive.startswith("n") else "ask"
            else:
                recursive = False
        else:
            printer("  (folder not found on this computer; you can fix it later in settings.yaml)")
    focus = _list(_ask("1-3 FOCUS keywords, comma separated (your core topics)", "", input_fn))[:3]
    secondary = _list(_ask("Up to 10 SECONDARY keywords, comma separated", "", input_fn))[:10]
    exclude = _list(_ask("Topics to EXCLUDE (optional)", "", input_fn))
    email = _ask("Your email (sent to CrossRef/Unpaywall as polite contact; optional)", "", input_fn)
    abbrev_files = []
    if _ask("Use the general abbreviation list (Machine Learning -> ML, ...)? (y/n)", "n", input_fn).lower().startswith("y"):
        abbrev_files.append("config/abbreviations/general.yaml")

    block = project_block(name, library, recursive, focus, secondary, exclude, abbrev_files)
    target = Path(target) if target else (settings.path or Path.cwd() / "config" / "settings.yaml")
    if target.exists():
        printer(f"\n{target} already exists. Add this under 'projects:' in that file:\n")
        printer("  " + yaml.safe_dump(block, sort_keys=False, allow_unicode=True).replace("\n", "\n  "))
        return 0
    example = Path(__file__).resolve().parent.parent / "config" / "settings.example.yaml"
    header = ("# litsuite settings, created by 'litsuite init'.\n"
              f"# Every option is explained in {example.name} and in README.md.\n\n")
    data = {"user": {"email": email}, "projects": block, "email_accounts": {},
            "access": {"institution": "", "idp_entity_id": "", "ezproxy_prefix": "", "openathens_domain": ""}}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    printer(f"\nCreated {target}")
    printer(f"Next: litsuite catalog create --project {name}")
    return 0
