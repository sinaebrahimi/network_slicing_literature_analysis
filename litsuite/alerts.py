"""Email alerts -> relevant new papers -> PDFs in your library, renamed and cataloged.

Pipeline for one project:

1. read UNREAD alert emails (Google Scholar, ScienceDirect, IEEE, ACM, Springer...) from
   the allowed senders in the project's email accounts; already-read emails are never
   opened, and emails processed in earlier runs are skipped
2. extract candidate papers, merge duplicates across emails
3. drop papers already in the library (DOI or title match)
4. score relevance (keywords + similarity to your library)
5. choose: ``ask`` (you pick), ``auto`` (score >= auto_download_score) or ``report`` (nothing)
6. for each chosen paper: look up metadata, build the filename from your naming
   convention + abbreviations, download (open access first, then your signed-in
   publisher session), save it to the library folder, add a catalog row
7. write an audit report (Markdown + CSV) and remember what was decided; accounts with
   ``mark_as_read: true`` get their processed alert emails marked as read
   (``report`` mode is a dry run: it changes nothing and does not use up any email)
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from litsuite.catalog import make_ref_key, read_catalog, write_catalog, backup_file
from litsuite.keywords import build_scorer
from litsuite.mail import open_mailbox
from litsuite.mail.parsers import AlertCandidate, all_sources, match_source, parse_message
from litsuite.metadata import (PaperMeta, arxiv_by_id, complete, crossref_by_doi, crossref_by_pii,
                               crossref_search, s2_by_id, s2_search)
from litsuite.naming import build_filename, parse_filename, unique_name
from litsuite.textutils import title_key, title_similarity

log = logging.getLogger(__name__)
STATE_FILE = "alerts_state.json"
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# State (what has already been processed / decided)
# ---------------------------------------------------------------------------
class AlertState:
    def __init__(self, path: Path):
        self.path = Path(path)
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        self.processed: dict = data.get("processed_messages", {})
        self.decisions: dict = data.get("decisions", {})
        self.pending: list = data.get("pending", [])

    def save(self) -> None:
        cutoff = (dt.date.today() - dt.timedelta(days=365)).isoformat()
        self.processed = {k: v for k, v in self.processed.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"processed_messages": self.processed, "decisions": self.decisions,
                                         "pending": self.pending}, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Library lookup
# ---------------------------------------------------------------------------
class LibraryMatcher:
    """Is a paper already in the library? Matches DOIs, then titles compared in a
    canonical (abbreviated) form so "... Network Slicing ..." finds your "... NS ..." file."""

    def __init__(self, rows, abbreviator=None):
        self.abbreviator = abbreviator
        self.dois = {}
        self.keys = {}
        for r in rows:
            if r.get("status") == "missing":
                continue
            if r.get("doi"):
                self.dois[r["doi"].lower()] = r
            for t in (r.get("title_official", ""), r.get("title_raw", ""),
                      parse_filename(r.get("filename", "")).title_raw):
                for k in self._keys(t):
                    self.keys.setdefault(k, r)

    def _keys(self, title: str) -> list:
        forms = [title] + ([self.abbreviator.abbreviate(title)] if self.abbreviator and title else [])
        return [k for k in dict.fromkeys(title_key(f) for f in forms) if len(k.split()) >= 3]

    def find(self, title: str = "", doi: str = ""):
        if doi and doi.lower() in self.dois:
            return self.dois[doi.lower()]
        keys = self._keys(title)
        for k in keys:
            if k in self.keys:
                return self.keys[k]
        for k in keys:
            for other, r in self.keys.items():
                if abs(len(other) - len(k)) < 25 and title_similarity(other, k) >= 0.92:
                    return r
        return None

    def add(self, row: dict) -> None:
        if row.get("doi"):
            self.dois[row["doi"].lower()] = row
        for k in self._keys(row.get("title_official", "")):
            self.keys[k] = row


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    candidate: AlertCandidate
    score: float = 0.0
    reasons: list = field(default_factory=list)
    status: str = "candidate"    # in_library | seen_before | low_score | skipped | downloaded | needs_manual | report_only
    filename: str = ""
    saved_to: str = ""
    doi: str = ""
    venue: str = ""
    method: str = ""
    note: str = ""
    links: list = field(default_factory=list)


def collect_candidates(project, settings, accounts=None, since_days=None, state=None, reprocess=False,
                       mailbox_factory=open_mailbox, printer=print) -> tuple:
    sources = all_sources(settings.data.get("alert_sources"))
    enabled = project.alerts.get("sources") or list(sources)
    senders = sorted({s for name in enabled for s in (sources.get(name) or {}).get("senders", [])})
    since = dt.date.today() - dt.timedelta(days=int(since_days or project.alerts.get("since_days") or 14))
    merged: dict = {}
    seen_msgs = []
    handled: dict = {}   # account -> messages to mark as read at the end of the run
    for acc in accounts or project.alerts.get("accounts") or []:
        cfg = settings.email_account(acc)
        try:
            mb = mailbox_factory(cfg)
        except Exception as e:
            printer(f"  ! cannot open email account '{acc}': {e}")
            continue
        n_msgs = 0
        try:
            for msg in mb.search(senders, since):
                if msg.read:   # never open an email that has already been read
                    continue
                if not reprocess and state is not None and msg.id in state.processed:
                    continue
                src = match_source(msg, sources, enabled)
                if not src:
                    continue
                n_msgs += 1
                seen_msgs.append(msg.id)
                handled.setdefault(acc, []).append(msg)
                for c in parse_message(msg, src, sources):
                    if c.key in merged:
                        merged[c.key].absorb(c)
                    else:
                        merged[c.key] = c
        finally:
            mb.close()
        printer(f"  {acc}: {n_msgs} new unread alert email(s)")
    return list(merged.values()), seen_msgs, handled


def mark_handled_as_read(settings, handled: dict, mailbox_factory=open_mailbox, printer=print) -> int:
    """Mark processed alert emails as read, for accounts with ``mark_as_read: true``."""
    total = 0
    for acc, msgs in handled.items():
        cfg = settings.email_account(acc)
        if not cfg.get("mark_as_read") or not msgs:
            continue
        try:
            mb = mailbox_factory(cfg)
            try:
                total += mb.mark_read(msgs)
            finally:
                mb.close()
        except Exception as e:
            printer(f"  ! could not mark emails as read in '{acc}': {e}")
    return total


def resolve_metadata(c: AlertCandidate, http=None, online: bool = True) -> PaperMeta:
    meta = None
    if online:
        if c.doi:
            meta = crossref_by_doi(c.doi, http)
        if meta is None and c.arxiv_id:
            meta = arxiv_by_id(c.arxiv_id, http)
            if meta is not None:
                s2 = s2_by_id(f"ARXIV:{c.arxiv_id}", http)
                published_doi = (meta.doi or (s2.doi if s2 else "")) or ""
                if published_doi and not published_doi.lower().startswith("10.48550"):
                    cr = crossref_by_doi(published_doi, http)
                    meta = cr.merge(meta) if cr else meta
                if s2:
                    meta.merge(s2)
        if meta is None and c.pii:
            meta = crossref_by_pii(c.pii, http)
        if meta is None:
            meta = crossref_search(c.title, c.authors[0] if c.authors else "", c.year, http) \
                or s2_search(c.title, http=http)
    fallback = PaperMeta(title=c.title, authors=list(c.authors), year=c.year or c.message_date[:4],
                         venue_full=c.venue_line,
                         doi=c.doi, abstract=c.snippet if len(c.snippet) > 300 else "", arxiv_id=c.arxiv_id,
                         url=c.url)
    if c.arxiv_id and not c.doi:
        fallback.pub_type = "preprint"
    if meta is None:
        meta = fallback
    else:
        meta.merge(fallback)
        if title_similarity(meta.title, c.title) < 0.6:   # lookup found something else: trust the email
            meta = fallback
    if online:
        meta = complete(meta, http)
    return meta


def _venue_from_line(venue_line: str) -> str:
    """'IEEE Transactions on Network and Service Management, 2025' -> venue name only."""
    v = re.sub(r",?\s*(19|20)\d{2}\s*$", "", venue_line or "").strip(" ,.…")
    return v.replace("…", "").strip()


def process_candidate(d: Decision, project, ctx: dict) -> None:
    """Resolve, name, download and file one chosen paper (updates ``d`` in place)."""
    c = d.candidate
    meta = resolve_metadata(c, ctx.get("http"), ctx.get("online", True))
    d.doi = meta.doi
    dup = ctx["library"].find(meta.title, meta.doi)
    if dup is not None:
        d.status, d.note = "in_library", f"already in library as {dup.get('filename')}"
        return
    resolver = ctx["resolver"]
    venue = resolver.resolve(meta.doi, meta.venue_full or _venue_from_line(c.venue_line), meta.pub_type,
                             meta.event_acronym)
    d.venue = venue
    year = meta.year or c.year
    author = meta.first_author or (c.authors[0] if c.authors else "")
    name = build_filename(venue, year, author, meta.title or c.title, project.abbreviator, project.naming)
    name = unique_name(name, ctx["existing_names"])
    d.filename = name
    tmp_dir = project.state_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / "download.pdf"
    ids = {"doi": meta.doi, "arnumber": c.ieee_arnumber, "pii": c.pii, "arxiv_id": meta.arxiv_id or c.arxiv_id,
           "url": c.url}
    res = ctx["downloader"].download(meta, tmp, ids=ids, direct_pdf=c.pdf_url, use_browser=ctx.get("use_browser", True))
    d.links = [u for u in [c.url, f"https://doi.org/{meta.doi}" if meta.doi else ""] if u]
    if not res.ok:
        d.status, d.note = "needs_manual", res.note
        return
    folder = (project.alerts.get("download_folder") or "").strip("/\\")
    rel = str(PurePosixPath(folder) / name) if folder else name
    d.saved_to = ctx["storage"].save(tmp, rel)
    tmp.unlink(missing_ok=True)
    d.status, d.method = "downloaded", res.method
    ctx["existing_names"].add(name)
    if project.alerts.get("add_to_catalog", True):
        row = {
            "title_official": meta.title or c.title, "venue_acronym": venue, "year": year, "doi": meta.doi,
            "abstract": meta.abstract, "first_author": author, "filename": name, "venue_full": meta.venue_full,
            "pub_type": meta.pub_type, "subfolder": folder, "relative_path": rel, "library": "",
            "status": "present", "added_on": dt.date.today().isoformat(), "source": f"alert:{c.source}",
            "title_raw": parse_filename(name).title_raw,
        }
        row["reference_key"] = make_ref_key(venue, year, author)
        ctx["rows"].append(row)
        ctx["library"].add(row)


def parse_selection(text: str, n: int) -> list:
    """'1,3-5' -> [0, 2, 3, 4]."""
    out = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.match(r"^(\d+)-(\d+)$", part)
        rng = range(int(m.group(1)), int(m.group(2)) + 1) if m else [int(part)] if part.isdigit() else []
        out += [i - 1 for i in rng if 1 <= i <= n]
    return sorted(set(out))


def ask_user(shown: list, preselected: list, input_fn=input, printer=print) -> list:
    printer("")
    for i, d in enumerate(shown, 1):
        c = d.candidate
        mark = "*" if i - 1 in preselected else " "
        venue = f" | {c.venue_line[:45]}" if c.venue_line else ""
        printer(f"{mark}{i:>3}. [{d.score:5.2f}] {c.title[:110]}{venue}")
        printer(f"        {', '.join(r for r in d.reasons if not r.startswith('similarity'))[:110]}")
    printer("\n* = above your auto-download score.")
    ans = input_fn("Download which? [Enter = the * ones | a = all | n = none | numbers e.g. 1,3-5]: ").strip().lower()
    if ans == "":
        return preselected
    if ans in ("a", "all"):
        return list(range(len(shown)))
    if ans in ("n", "none", "q"):
        return []
    return parse_selection(ans, len(shown))


def write_reports(project, decisions: list, run_info: dict) -> tuple:
    project.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = project.reports_dir / f"alerts-{stamp}.md"
    csv_path = project.reports_dir / f"alerts-{stamp}.csv"
    cols = ["status", "score", "title", "filename", "venue", "doi", "method", "note", "source", "url",
            "message_date", "message_subject", "reasons", "saved_to"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for d in decisions:
            c = d.candidate
            w.writerow({"status": d.status, "score": d.score, "title": c.title, "filename": d.filename,
                        "venue": d.venue, "doi": d.doi or c.doi, "method": d.method, "note": d.note,
                        "source": c.source, "url": c.url, "message_date": c.message_date,
                        "message_subject": c.message_subject, "reasons": "; ".join(d.reasons),
                        "saved_to": d.saved_to})
    sections = [("downloaded", "Downloaded"), ("needs_manual", "Needs manual download"),
                ("report_only", "Relevant (report mode, not downloaded)"), ("skipped", "Shown but not chosen"),
                ("low_score", "Below min_score"), ("in_library", "Already in library")]
    lines = [f"# Alert report: {project.name}", "", f"Run: {run_info.get('when')}  |  mode: {run_info.get('mode')}  |  "
             f"emails: {run_info.get('emails')}  |  candidates: {len(decisions)}", ""]
    for status, heading in sections:
        group = sorted((d for d in decisions if d.status == status), key=lambda d: -d.score)
        if not group:
            continue
        lines += [f"## {heading} ({len(group)})", ""]
        for d in group:
            c = d.candidate
            link = d.links[0] if d.links else c.url
            line = f"- **{d.score:.2f}** [{c.title}]({link})" if link else f"- **{d.score:.2f}** {c.title}"
            if d.filename and status == "downloaded":
                line += f"  \n  -> `{d.filename}` ({d.method})"
            elif d.filename:
                line += f"  \n  suggested name: `{d.filename}`"
            if d.note:
                line += f"  \n  {d.note}"
            lines.append(line)
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log_path = project.data_dir / "downloads_log.csv"
    new_log = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8-sig" if new_log else "utf-8") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["date", "status", "score", "title", "filename", "doi", "method", "source", "url", "note"])
        for d in decisions:
            if d.status in ("downloaded", "needs_manual"):
                c = d.candidate
                w.writerow([dt.date.today().isoformat(), d.status, d.score, c.title, d.filename, d.doi or c.doi,
                            d.method, c.source, c.url, d.note])
    return md_path, csv_path


def run_alerts(project, settings, *, accounts=None, since_days=None, mode=None, reprocess=False,
               use_browser=True, online=True, input_fn=input, printer=print,
               mailbox_factory=open_mailbox, downloader=None, storage=None, http=None) -> dict:
    from litsuite.fetch.downloader import PaperDownloader
    from litsuite.storage import open_storage

    alerts = project.alerts
    mode = (mode or alerts.get("mode") or "ask").lower()
    if mode not in ("ask", "auto", "report"):
        raise ValueError("mode must be ask, auto or report")
    rows, fieldnames = read_catalog(project.catalog_path)
    library = LibraryMatcher(rows, project.abbreviator)
    scorer = build_scorer(project, rows)
    state = AlertState(project.state_dir / STATE_FILE)

    printer(f"Project '{project.name}': reading alert emails...")
    cands, msg_ids, handled = collect_candidates(project, settings, accounts, since_days, state, reprocess,
                                                 mailbox_factory, printer)
    known = {c.key for c in cands}
    for p in state.pending:   # retry earlier failed downloads
        try:
            c = AlertCandidate(**{k: v for k, v in p.items() if k in AlertCandidate.__dataclass_fields__})
        except TypeError:
            continue
        if c.key not in known:
            cands.append(c)
            known.add(c.key)

    decisions = []
    for c in cands:
        d = Decision(c)
        if library.find(c.title, c.doi) is not None:
            d.status = "in_library"
        elif not reprocess and c.key in state.decisions and state.decisions[c.key].get("status") != "needs_manual":
            d.status = "seen_before"
        else:
            d.score, d.reasons = scorer.score(c.title, c.snippet)
            d.status = "candidate" if d.score >= float(alerts.get("min_score", 2.0)) else "low_score"
        decisions.append(d)

    shown = sorted((d for d in decisions if d.status == "candidate"), key=lambda d: -d.score)
    auto_thr = float(alerts.get("auto_download_score", 4.0))
    limit = int(alerts.get("max_downloads", 25))
    pre = [i for i, d in enumerate(shown) if d.score >= auto_thr][:limit]
    if not shown:
        chosen = []
    elif mode == "report":
        chosen = []
        for d in shown:
            d.status = "report_only"
    elif mode == "auto":
        chosen = pre
    else:
        chosen = ask_user(shown, pre, input_fn, printer)[:limit]

    if chosen:
        specs = project.library_specs
        if not specs:
            raise ValueError(f"project '{project.name}' has no 'library' folder to save PDFs into")
        storage = storage or open_storage(specs[0], settings)
        try:
            existing = {f.name for f in storage.list_files(recursive=True)}
        except Exception:
            existing = set()
        downloader = downloader or PaperDownloader(settings.access, settings.user_email, http)
        ctx = {"http": http, "online": online, "library": library, "resolver": project.venue_resolver(rows),
               "existing_names": existing, "downloader": downloader, "storage": storage, "rows": rows,
               "use_browser": use_browser}
        try:
            for n, i in enumerate(chosen, 1):
                d = shown[i]
                printer(f"[{n}/{len(chosen)}] {d.candidate.title[:90]}")
                try:
                    process_candidate(d, project, ctx)
                except Exception as e:
                    d.status, d.note = "needs_manual", f"error: {e}"
                    log.exception("processing failed")
                printer(f"   -> {d.status}: {d.filename or ''} {d.note}".rstrip())
        finally:
            downloader.close()
        if any(d.status == "downloaded" for d in decisions) and project.alerts.get("add_to_catalog", True):
            backup_file(project.catalog_path, project.backups_dir)
            write_catalog(project.catalog_path, rows, fieldnames)
    for d in shown:
        if d.status == "candidate":
            d.status = "skipped"

    today = dt.date.today().isoformat()
    if mode != "report":   # report mode is a dry run: no email is used up, nothing is remembered
        for mid in msg_ids:
            state.processed[mid] = today
        pending = []
        for d in decisions:
            if d.status in ("downloaded", "skipped", "low_score", "in_library"):
                state.decisions[d.candidate.key] = {"status": d.status, "date": today, "score": d.score,
                                                    "title": d.candidate.title}
            if d.status == "needs_manual":
                attempts = state.decisions.get(d.candidate.key, {}).get("attempts", 0) + 1
                state.decisions[d.candidate.key] = {"status": "needs_manual", "date": today, "score": d.score,
                                                    "title": d.candidate.title, "attempts": attempts}
                if attempts < MAX_ATTEMPTS:
                    pending.append(asdict(d.candidate))
        state.pending = pending
        state.save()
        marked = mark_handled_as_read(settings, handled, mailbox_factory, printer)
        if marked:
            printer(f"Marked {marked} processed alert email(s) as read.")

    visible = [d for d in decisions if d.status != "seen_before"]
    md = csv_path = ""
    if visible:   # no report for runs without new alerts (keeps scheduled runs tidy)
        md, csv_path = write_reports(project, visible, {"when": dt.datetime.now().isoformat(timespec="seconds"),
                                                        "mode": mode, "emails": len(msg_ids)})
    counts: dict = {}
    for d in visible:
        counts[d.status] = counts.get(d.status, 0) + 1
    return {"counts": counts, "report": str(md), "report_csv": str(csv_path), "decisions": visible}
