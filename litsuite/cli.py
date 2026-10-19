"""Command line: ``litsuite <command>`` (or ``python -m litsuite``). Run with no
arguments for an interactive menu. ``litsuite <command> -h`` shows every option."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
from pathlib import Path

from litsuite import __version__
from litsuite.config import ConfigError, Settings

log = logging.getLogger("litsuite")


# ---------------------------------------------------------------------------
# Small interactive helpers
# ---------------------------------------------------------------------------
def ask(prompt: str, default: str = "", input_fn=input) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input_fn(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def ask_yes_no(prompt: str, default: bool = True, input_fn=input) -> bool:
    ans = ask(f"{prompt} ({'Y/n' if default else 'y/N'})", "", input_fn).lower()
    return default if not ans else ans.startswith("y")


def _clean_path(text: str) -> str:
    """Accept paths pasted with quotes (Windows 'Copy as path')."""
    return text.strip().strip('"').strip("'")


def setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)
    for noisy in ("urllib3", "googleapiclient", "msal", "dropbox", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Resolving which project / folder a command works on
# ---------------------------------------------------------------------------
def get_project(args, settings: Settings, input_fn=input):
    """Project from --project, or an ad-hoc one from --folder/--output, or ask."""
    folder = getattr(args, "folder", None)
    output = getattr(args, "output", None)
    if getattr(args, "project", None):
        prj = settings.project(args.project)
        if folder:
            prj.cfg["library"] = folder
        if output:
            out = Path(output).expanduser().resolve()
            prj.cfg["data_dir"], prj.cfg["catalog"] = str(out.parent), out.name
        return prj
    if folder or output:
        return settings.adhoc_project(folder, output)
    names = settings.project_names
    if len(names) == 1:
        return settings.project(names[0])
    if names:
        print("Projects: " + ", ".join(f"{i}) {n}" for i, n in enumerate(names, 1)))
        choice = ask("Which project? (number or name)", "1", input_fn)
        name = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) else choice
        return settings.project(name)
    return settings.adhoc_project(None, None)


def ensure_library(project, input_fn=input) -> None:
    if project.library_specs:
        return
    print("Which folder holds your PDFs? You can paste a path (Windows, macOS or Linux), or a cloud")
    print("location such as dropbox:/Research/Papers, gdrive:/Papers, onedrive:/Documents/Papers,")
    print("rclone:remote:Papers, or {onedrive}/Papers for the OneDrive desktop folder.")
    from litsuite.storage.sync_folders import detect_all

    found = detect_all()
    if found:
        print("Cloud folders found on this computer: " + "; ".join(f"{{{k}}} = {v[0]}" for k, v in found.items()))
    folder = _clean_path(ask("PDF folder", "", input_fn))
    if not folder:
        raise SystemExit("No folder given.")
    project.cfg["library"] = folder


def choose_recursion(project, storages, args, input_fn=input) -> tuple:
    """(recursive, include, exclude) from flags, settings, or by asking."""
    include = list(getattr(args, "include", None) or project.cfg.get("include_folders") or [])
    exclude = list(getattr(args, "exclude", None) or project.cfg.get("exclude_folders") or [])
    if getattr(args, "recursive", None) is not None:
        return args.recursive, include, exclude
    setting = project.recursive_setting
    if setting != "ask":
        return setting, include, exclude
    summary = {}
    for st in storages:
        for k, v in st.subfolder_summary().items():
            summary[k] = summary.get(k, 0) + v
    if not summary:
        return False, include, exclude
    print("\nThis folder has subfolders containing PDFs:")
    for name, n in sorted(summary.items()):
        print(f"  - {name}  ({n} PDFs)")
    ans = ask("Include PDFs from subfolders? [a]ll / [n]one / [s]elect", "a", input_fn).lower()
    if ans.startswith("n"):
        return False, include, exclude
    if ans.startswith("s"):
        picks = []
        for name in sorted(summary):
            if ask_yes_no(f"  include '{name}'?", True, input_fn):
                picks.append(name)
        return True, picks, exclude
    return True, include, exclude


def open_storages(project, settings):
    from litsuite.storage import open_storage

    return [open_storage(spec, settings) for spec in project.library_specs]


def print_catalog_summary(res: dict) -> None:
    print("\n" + "=" * 64)
    print(f"Catalog: {res['catalog']}")
    print(f"PDFs found: {res['files']}  |  rows in catalog: {res['total']}")
    print(f"Added: {len(res['added'])}  |  renamed/moved: {len(res['renamed'])}  |  "
          f"missing now: {len(res['missing'])}" + ("  (pruned)" if res.get("pruned") else ""))
    for old, new in res["renamed"][:10]:
        print(f"  moved: {old} -> {new}")
    for r in res["missing"][:10]:
        print(f"  missing: {r.get('relative_path') or r.get('filename')}")
    if res.get("published_preprints"):
        print("Preprints that are now published:")
        for r in res["published_preprints"]:
            print(f"  {r['filename']}  ->  {r['published_venue']} ({r['doi']})")
    if res.get("duplicates"):
        print(f"Possible duplicates: {len(res['duplicates'])} groups (litsuite catalog duplicates)")
    added = res["added"]
    if added:
        with_doi = sum(1 for r in added if r.get("doi"))
        with_abs = sum(1 for r in added if r.get("abstract"))
        print(f"New rows with DOI: {with_doi}/{len(added)}, with abstract: {with_abs}/{len(added)}")
    print("=" * 64)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_catalog(args, settings, input_fn=input) -> int:
    from litsuite.catalog import find_duplicates, read_catalog, scan_libraries, update_catalog
    from litsuite.http import default_http

    project = get_project(args, settings, input_fn)
    if args.action == "duplicates":
        rows, _ = read_catalog(project.catalog_path)
        dups = find_duplicates(rows)
        for group in dups:
            print("  " + "\n  ".join(group) + "\n")
        print(f"{len(dups)} group(s) of possible duplicates.")
        return 0
    ensure_library(project, input_fn)
    if args.action == "create" and project.catalog_path.exists() and not args.force:
        if not ask_yes_no(f"{project.catalog_path} already exists. Rebuild it from scratch? "
                          "(say 'n' and run 'catalog update' to only add new files)", False, input_fn):
            return 1
        project.catalog_path.rename(project.catalog_path.with_suffix(
            f".before-rebuild-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"))
    storages = open_storages(project, settings)
    recursive, include, exclude = choose_recursion(project, storages, args, input_fn)
    files = scan_libraries(storages, recursive, include, exclude)
    print(f"Library: {', '.join(project.library_specs)}  ({len(files)} PDFs"
          f"{', subfolders included' if recursive else ', top level only'})")
    print(f"Catalog: {project.catalog_path}")
    existing_rows, _ = read_catalog(project.catalog_path)
    res = update_catalog(project.catalog_path, files, backups_dir=project.backups_dir,
                         enrich=not args.offline, prune=getattr(args, "prune", False),
                         retry_incomplete=getattr(args, "retry_incomplete", False),
                         http=default_http(settings.user_email), resolver=project.venue_resolver(existing_rows))
    print_catalog_summary(res)
    if res["published_preprints"]:
        project.reports_dir.mkdir(parents=True, exist_ok=True)
        with open(project.reports_dir / "published_preprints.txt", "a", encoding="utf-8") as f:
            for r in res["published_preprints"]:
                f.write(f"{dt.date.today()} | {r['filename']} | now in: {r['published_venue']} | {r['doi']}\n")
    return 0


def cmd_abstracts(args, settings, input_fn=input) -> int:
    from litsuite.catalog import fill_abstracts
    from litsuite.http import default_http

    if args.input:
        path, backups = Path(args.input).expanduser(), None
    else:
        project = get_project(args, settings, input_fn)
        path, backups = project.catalog_path, project.backups_dir
    res = fill_abstracts(path, args.output_csv, backups, default_http(settings.user_email))
    print(f"Filled {res['filled']}/{res['missing_before']} missing abstracts -> {res['output']}")
    print(f"Sources: {res['sources']}")
    return 0


def cmd_keywords(args, settings, input_fn=input) -> int:
    from litsuite.catalog import read_catalog
    from litsuite.keywords import build_scorer, suggest_keywords, write_learned_keywords

    project = get_project(args, settings, input_fn)
    rows, _ = read_catalog(project.catalog_path)
    if args.action == "suggest":
        sugg = suggest_keywords(rows, args.top)
        print(f"Top phrases in {len(rows)} papers of '{project.name}' (number of papers):")
        for k, c in sugg:
            print(f"  {k:45s} {c}")
        if args.save:
            path = write_learned_keywords(sugg, project.learned_keywords_path)
            print(f"\nSaved to {path} (used with a small weight; edit freely).")
        else:
            print("\nAdd --save to store them as learned keywords, or copy the best ones into settings.yaml.")
        return 0
    if args.action == "show":
        kw = project.keywords
        print(f"focus:     {kw.get('focus')}")
        print(f"secondary: {kw.get('secondary')}")
        print(f"exclude:   {kw.get('exclude')}")
        from litsuite.keywords import load_learned_keywords

        print(f"learned:   {load_learned_keywords(project.learned_keywords_path)} "
              f"(use_learned={kw.get('use_learned', True)})")
        return 0
    scorer = build_scorer(project, rows)
    score, reasons = scorer.score(args.title, args.snippet or "")
    alerts = project.alerts
    print(f"score = {score}   (shown if >= {alerts['min_score']}, auto-downloaded if >= "
          f"{alerts['auto_download_score']})")
    for r in reasons:
        print("  " + r)
    return 0


def cmd_abbrev(args, settings, input_fn=input) -> int:
    from litsuite.abbreviations import learn_abbreviations, write_suggestions
    from litsuite.catalog import read_catalog
    from litsuite.naming import format_title

    project = get_project(args, settings, input_fn)
    ab = project.abbreviator
    if args.action == "list":
        print(f"{len(ab)} abbreviations from: {', '.join(str(p) for p in project.abbreviation_files) or '(none)'}")
        for phrase, short in sorted(ab.mapping.items(), key=lambda kv: kv[1].lower()):
            print(f"  {short:10s} <- {phrase}")
        return 0
    if args.action == "test":
        print("abbreviated: " + ab.abbreviate(args.title))
        for orig, short in ab.explain(args.title):
            print(f"  '{orig}' -> {short}")
        print("in filename: " + format_title(args.title, ab, project.naming))
        return 0
    rows, _ = read_catalog(project.catalog_path)
    sugg = learn_abbreviations(rows, args.min_count)
    if not sugg:
        print("No abbreviations found (the catalog needs filenames and official titles).")
        return 0
    for phrase, short, n, example in sugg:
        state = "already set" if ab.mapping.get(phrase) == short else "new"
        print(f"  {short:8s} <- {phrase:40s} {n:4d} files  ({state})")
    if args.save:
        path = write_suggestions(sugg, project.data_dir / "abbreviations_learned.yaml")
        print(f"\nSaved for review: {path}\nCopy the lines you want into your abbreviation file "
              f"or add this file under 'abbreviations:' in settings.yaml.")
    return 0


def cmd_rename(args, settings, input_fn=input) -> int:
    """Preview (and later apply) renaming library files to the naming convention."""
    from litsuite.catalog import backup_file, read_catalog, write_catalog
    from litsuite.naming import build_filename, parse_filename, unique_name
    from litsuite.storage import open_storage

    project = get_project(args, settings, input_fn)
    rows, fields = read_catalog(project.catalog_path)
    if args.apply:
        plan = list(csv.DictReader(open(args.apply, encoding="utf-8-sig")))
        storage = open_storage(project.library_specs[0], settings)
        by_rel = {(r.get("relative_path") or r.get("filename")): r for r in rows}
        done = 0
        for p in plan:
            old, new = p["current"], p["proposed"]
            if not new or old == new:
                continue
            storage.rename(old, new)
            r = by_rel.get(old)
            if r:
                r.update(relative_path=new, filename=Path(new).name, title_raw=parse_filename(new).title_raw)
            done += 1
            print(f"  {old}  ->  {new}")
        backup_file(project.catalog_path, project.backups_dir)
        write_catalog(project.catalog_path, rows, fields)
        print(f"Renamed {done} file(s); catalog updated.")
        return 0
    taken = {r.get("filename", "") for r in rows}
    plan = []
    for r in rows:
        if r.get("status") == "missing" or not r.get("title_official") or not r.get("year"):
            continue
        if args.only_alerts and not (r.get("source") or "").startswith("alert"):
            continue
        name = build_filename(r.get("venue_acronym", ""), r["year"], r.get("first_author", ""),
                              r["title_official"], project.abbreviator, project.naming)
        if name != r.get("filename"):
            name = unique_name(name, taken - {r.get("filename", "")})
            rel = r.get("relative_path") or r.get("filename")
            folder = str(Path(rel).parent).replace("\\", "/")
            plan.append({"current": rel, "proposed": f"{folder}/{name}" if folder not in ("", ".") else name})
    project.reports_dir.mkdir(parents=True, exist_ok=True)
    out = project.reports_dir / f"rename-plan-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["current", "proposed"])
        w.writeheader()
        w.writerows(plan)
    for p in plan[:15]:
        print(f"  {p['current']}\n    -> {p['proposed']}")
    print(f"\n{len(plan)} file(s) would be renamed. Plan written to:\n  {out}")
    print("Review/edit it (delete rows you want to keep as they are), then run:\n"
          f"  litsuite rename --project {project.name} --apply \"{out}\"")
    return 0


def _alert_projects(args, settings, input_fn):
    if getattr(args, "all_projects", False):
        return [settings.project(n) for n in settings.project_names]
    return [get_project(args, settings, input_fn)]


def cmd_alerts(args, settings, input_fn=input) -> int:
    from litsuite.alerts import run_alerts
    from litsuite.http import default_http

    for project in _alert_projects(args, settings, input_fn):
        if not (args.account or project.alerts.get("accounts")):
            print(f"Project '{project.name}' has no alert email accounts (alerts.accounts in settings.yaml).")
            continue
        res = run_alerts(project, settings, accounts=args.account, since_days=args.since_days, mode=args.mode,
                         reprocess=args.reprocess, use_browser=not args.no_browser, input_fn=input_fn,
                         http=default_http(settings.user_email))
        print(f"\nSummary for '{project.name}': {res['counts'] or 'no new papers'}")
        if res["report"]:
            print(f"Report: {res['report']}")
    return 0


def cmd_download(args, settings, input_fn=input) -> int:
    """Add one paper (by DOI, URL, arXiv ID or title) to a project, named by convention."""
    from litsuite.alerts import Decision, LibraryMatcher, process_candidate
    from litsuite.catalog import backup_file, read_catalog, write_catalog
    from litsuite.fetch.downloader import PaperDownloader
    from litsuite.http import default_http
    from litsuite.mail.links import identify
    from litsuite.mail.parsers import AlertCandidate
    from litsuite.storage import open_storage

    project = get_project(args, settings, input_fn)
    if not project.library_specs:
        ensure_library(project, input_fn)
    info = identify(args.url or "") if args.url else {}
    c = AlertCandidate(title=args.title or "", url=args.url or "", doi=args.doi or info.get("doi", ""),
                       arxiv_id=args.arxiv or info.get("arxiv", ""), ieee_arnumber=info.get("ieee", ""),
                       pii=info.get("pii", ""), source="manual")
    if info.get("is_pdf"):
        c.pdf_url = args.url
    rows, fields = read_catalog(project.catalog_path)
    storage = open_storage(project.library_specs[0], settings)
    http = default_http(settings.user_email)
    ctx = {"http": http, "online": True, "library": LibraryMatcher(rows, project.abbreviator),
           "resolver": project.venue_resolver(rows), "existing_names": {f.name for f in storage.list_files()},
           "downloader": PaperDownloader(settings.access, settings.user_email, http), "storage": storage,
           "rows": rows, "use_browser": not args.no_browser}
    d = Decision(c)
    try:
        process_candidate(d, project, ctx)
    finally:
        ctx["downloader"].close()
    print(f"{d.status}: {d.filename} {d.note}".strip())
    if d.status == "downloaded":
        print(f"saved to {d.saved_to}")
        backup_file(project.catalog_path, project.backups_dir)
        write_catalog(project.catalog_path, rows, fields)
    elif d.links:
        print("Open manually: " + "  ".join(d.links))
    return 0 if d.status == "downloaded" else 2


def cmd_login(args, settings, input_fn=input) -> int:
    from litsuite import login

    if args.target == "email":
        if not args.name:
            names = list((settings.data.get("email_accounts") or {}))
            raise ConfigError(f"Which account? litsuite login email <name>   (configured: {', '.join(names) or 'none'})")
        print(login.login_email(settings, args.name, input_fn))
    elif args.target == "publisher":
        names = args.name.split(",") if args.name else ["all"]
        done = login.login_publishers(settings, names, args.autofill, input_fn)
        print(f"\nSaved browser session for: {', '.join(done)}. Check with: litsuite login check")
    elif args.target == "cloud":
        print(login.login_cloud(settings, args.name or "", input_fn))
    elif args.target == "sso":
        print(login.store_sso_credentials(input_fn))
    elif args.target == "check":
        login.check_publishers(settings, args.name.split(",") if args.name else None)
    elif args.target == "all":
        accounts = list((settings.data.get("email_accounts") or {}))
        for name in accounts:
            if (settings.email_account(name).get("provider") or "") == "folder":
                continue
            if ask_yes_no(f"Sign in to email account '{name}' now?", True, input_fn):
                try:
                    print(login.login_email(settings, name, input_fn))
                except Exception as e:   # keep going with the other accounts
                    print(f"  ! {name}: {e}")
        if (settings.access.get("method") or "browser") == "browser" and \
                ask_yes_no("Sign in to publisher websites (IEEE, ScienceDirect, Springer...) now?", True, input_fn):
            done = login.login_publishers(settings, ["all"], args.autofill, input_fn)
            print(f"Saved browser session for: {', '.join(done)}")
            login.check_publishers(settings)
        print("\n".join(login.status_lines(settings)))
    else:
        print("\n".join(login.status_lines(settings)))
    return 0


def cmd_projects(args, settings, input_fn=input) -> int:
    from litsuite.catalog import read_catalog

    print(f"Settings: {settings.path or '(none found: run litsuite init)'}")
    for name in settings.project_names:
        prj = settings.project(name)
        rows, _ = read_catalog(prj.catalog_path)
        present = sum(1 for r in rows if r.get("status") != "missing")
        print(f"\n[{name}] {prj.cfg.get('description', '')}")
        try:
            libs = ", ".join(prj.library_specs) or "(not set)"
        except ConfigError as e:
            libs = f"{prj.cfg.get('library')}  <- {e}"
        print(f"  library : {libs}")
        print(f"  catalog : {prj.catalog_path} ({present} papers)")
        print(f"  focus   : {', '.join(prj.keywords.get('focus') or []) or '-'}")
        print(f"  alerts  : {', '.join(prj.alerts.get('accounts') or []) or '-'} (mode {prj.alerts['mode']})")
    for w in settings.validate():
        print("warning: " + w)
    return 0


def cmd_where(args, settings, input_fn=input) -> int:
    from litsuite.storage.sync_folders import detect_all

    found = detect_all()
    if not found:
        print("No OneDrive / Google Drive / Dropbox / iCloud / Box sync folders found on this computer.")
    for k, paths in found.items():
        for p in paths:
            print(f"{{{k}}}  ->  {p}")
    return 0


def cmd_doctor(args, settings, input_fn=input) -> int:
    from litsuite import doctor

    return doctor.run(settings, online=args.online)


def cmd_init(args, settings, input_fn=input) -> int:
    from litsuite import setup_wizard

    return setup_wizard.run(settings, input_fn)


COMMANDS = {
    "catalog": cmd_catalog, "abstracts": cmd_abstracts, "keywords": cmd_keywords, "abbrev": cmd_abbrev,
    "rename": cmd_rename, "alerts": cmd_alerts, "download": cmd_download, "login": cmd_login,
    "projects": cmd_projects, "where": cmd_where, "doctor": cmd_doctor, "init": cmd_init,
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", default=argparse.SUPPRESS, help="project name from settings.yaml")
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to settings.yaml")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(prog="litsuite", parents=[common],
                                description="Maintain research literature libraries. Run without a command "
                                            "for an interactive menu.")
    p.add_argument("--version", action="version", version=f"litsuite {__version__}")
    sub = p.add_subparsers(dest="command")

    def folder_opts(sp):
        sp.add_argument("--folder", help="PDF folder (path or cloud location); asked if not set")
        sp.add_argument("--output", help="catalog CSV path (ad-hoc runs without a project)")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--recursive", dest="recursive", action="store_true", default=None,
                       help="include PDFs in subfolders")
        g.add_argument("--no-recursive", dest="recursive", action="store_false", help="top-level folder only")
        sp.add_argument("--include", action="append", help="only these subfolders (repeatable)")
        sp.add_argument("--exclude", action="append", help="skip these subfolders (repeatable)")
        sp.add_argument("--offline", action="store_true", help="do not look up DOIs/abstracts online")

    cat = sub.add_parser("catalog", parents=[common], help="build or update the CSV catalog of a PDF folder")
    cat_sub = cat.add_subparsers(dest="action", required=True)
    c1 = cat_sub.add_parser("create", parents=[common], help="build a new catalog from a folder")
    folder_opts(c1)
    c1.add_argument("--force", action="store_true", help="rebuild even if the catalog exists")
    c2 = cat_sub.add_parser("update", parents=[common], help="add new PDFs, follow renames, flag deleted files")
    folder_opts(c2)
    c2.add_argument("--prune", action="store_true", help="remove rows whose PDF was deleted (default: mark missing)")
    c2.add_argument("--retry-incomplete", action="store_true", help="retry lookups for rows without DOI/abstract")
    cat_sub.add_parser("duplicates", parents=[common], help="list papers that appear more than once")

    ab = sub.add_parser("abstracts", parents=[common], help="fill missing abstracts in a catalog")
    ab.add_argument("action", choices=["fill"])
    ab.add_argument("--input", help="catalog CSV (default: the project's catalog)")
    ab.add_argument("--output", dest="output_csv", help="write here instead of updating in place")

    kw = sub.add_parser("keywords", parents=[common], help="keyword suggestions and relevance testing")
    kw.add_argument("action", choices=["suggest", "show", "test"])
    kw.add_argument("title", nargs="?", default="", help="(test) a paper title")
    kw.add_argument("snippet", nargs="?", default="", help="(test) optional abstract/snippet")
    kw.add_argument("--top", type=int, default=40)
    kw.add_argument("--save", action="store_true", help="(suggest) save as learned keywords")

    abb = sub.add_parser("abbrev", parents=[common], help="title abbreviations (Network Slicing -> NS)")
    abb.add_argument("action", choices=["list", "test", "learn"])
    abb.add_argument("title", nargs="?", default="", help="(test) a title to abbreviate")
    abb.add_argument("--min-count", type=int, default=2, help="(learn) minimum number of files")
    abb.add_argument("--save", action="store_true", help="(learn) write suggestions to a YAML file for review")

    rn = sub.add_parser("rename", parents=[common], help="preview/apply renaming files to the naming convention")
    rn.add_argument("--apply", metavar="PLAN_CSV", help="apply a reviewed rename plan")
    rn.add_argument("--only-alerts", action="store_true", help="only files added from email alerts")

    al = sub.add_parser("alerts", parents=[common], help="find, choose and download new papers from email alerts")
    al.add_argument("--account", action="append", help="email account name(s) (default: the project's)")
    al.add_argument("--since-days", type=int, help="look back this many days (default from settings)")
    al.add_argument("--mode", choices=["ask", "auto", "report"],
                    help="ask=you choose, auto=by score, report=dry run (lists papers, changes nothing)")
    al.add_argument("--reprocess", action="store_true",
                    help="look again at UNREAD emails and papers already handled in earlier runs")
    al.add_argument("--no-browser", action="store_true", help="open-access downloads only")
    al.add_argument("--all-projects", action="store_true", help="run for every project")

    dl = sub.add_parser("download", parents=[common], help="add one paper by DOI / URL / arXiv ID / title")
    dl.add_argument("--doi")
    dl.add_argument("--url")
    dl.add_argument("--arxiv")
    dl.add_argument("--title")
    dl.add_argument("--no-browser", action="store_true")

    lg = sub.add_parser("login", parents=[common], help="sign in to email, publishers, cloud storage")
    lg.add_argument("target", nargs="?", default="status",
                    choices=["all", "email", "publisher", "cloud", "sso", "check", "status"],
                    help="all = guided sign-in to every email account and publisher")
    lg.add_argument("name", nargs="?", default="", help="account / publisher(s) e.g. ieee,elsevier / dropbox|gdrive|onedrive")
    lg.add_argument("--autofill", action="store_true", help="(publisher) pre-fill SSO forms with 'login sso' credentials")

    sub.add_parser("projects", parents=[common], help="list projects and check settings")
    sub.add_parser("where", parents=[common], help="show OneDrive/Google Drive/Dropbox folders on this computer")
    dr = sub.add_parser("doctor", parents=[common], help="check installation, settings and connectivity")
    dr.add_argument("--online", action="store_true", help="also test the scholarly APIs")
    sub.add_parser("init", parents=[common], help="guided setup: create settings.yaml with your first project")
    return p


MENU = [
    ("Update a catalog (add new PDFs)", ["catalog", "update"]),
    ("Create a catalog from a folder", ["catalog", "create"]),
    ("Fill missing abstracts", ["abstracts", "fill"]),
    ("Check email alerts and download relevant papers", ["alerts"]),
    ("Add one paper by DOI/URL", None),
    ("Suggest keywords from my library", ["keywords", "suggest"]),
    ("Sign-in status", ["login", "status"]),
    ("Guided setup (new project)", ["init"]),
    ("Check installation (doctor)", ["doctor"]),
]


def interactive_menu(input_fn=input) -> list:
    print(f"litsuite {__version__}: what would you like to do?")
    for i, (label, _) in enumerate(MENU, 1):
        print(f"  {i}) {label}")
    choice = ask("Choose", "1", input_fn)
    if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
        raise SystemExit("Nothing chosen.")
    argv = MENU[int(choice) - 1][1]
    if argv is None:
        ref = ask("DOI, article URL or arXiv ID", "", input_fn)
        argv = ["download", "--url" if ref.startswith("http") else "--arxiv" if ref[:4].isdigit() else "--doi", ref]
    return argv


def main(argv=None, input_fn=input) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in COMMANDS for a in argv) and not any(a in ("-h", "--help", "--version") for a in argv):
        argv += interactive_menu(input_fn)
    args = build_parser().parse_args(argv)
    for attr, default in (("project", None), ("config", None), ("verbose", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        settings = Settings.load(args.config)
        log_file = None
        if args.project and args.project in settings.project_names:
            log_file = settings.project(args.project).data_dir / "logs" / "litsuite.log"
        setup_logging(args.verbose, log_file)
        if args.command is None:
            build_parser().print_help()
            return 0
        return COMMANDS[args.command](args, settings, input_fn) or 0
    except (ConfigError, FileNotFoundError, PermissionError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted (progress so far is saved).", file=sys.stderr)
        return 130
    except Exception as e:  # auth / network errors from optional backends
        if type(e).__name__ in ("AuthError", "RuntimeError"):
            print(f"error: {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())

