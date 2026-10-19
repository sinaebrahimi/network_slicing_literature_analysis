"""Downloading (open access + signed-in browser session) and the full alerts pipeline.

A local web server plays the publisher: PDFs need a session cookie that is only
set after signing in through a login form, like institutional SSO.
"""

import csv
import json
from urllib.parse import urlparse

import pytest
import yaml

from conftest import chromium_available

import litsuite.alerts as alerts
from litsuite.alerts import AlertState, LibraryMatcher, parse_selection, run_alerts
from litsuite.catalog import read_catalog, scan_libraries, update_catalog
from litsuite.config import Settings
from litsuite.fetch.browser import BrowserSession, find_pdf_links, is_pdf
from litsuite.fetch.downloader import PaperDownloader
from litsuite.fetch.publishers import login_url, pdf_urls, publisher_for, publishers, via_proxy
from litsuite.http import Http
from litsuite.metadata import PaperMeta
from litsuite.storage import LocalStorage

needs_browser = pytest.mark.skipif(not chromium_available(), reason="Playwright Chromium not available")


class LocalOnlyHttp(Http):
    """Real HTTP for the local fake publisher; everything else is 'unreachable'."""

    def get(self, url, *a, **kw):
        if urlparse(url).hostname != "127.0.0.1":
            return None
        return super().get(url, *a, **kw)


# ---------------------------------------------------------------- publisher rules
def test_publisher_rules():
    t = publishers()
    assert publisher_for("10.1109/TNSM.2024.1") == "ieee"
    assert publisher_for("", "https://www.sciencedirect.com/science/article/pii/S1") == "elsevier"
    assert pdf_urls(t["ieee"], {"arnumber": "11012345"}) == [
        "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=11012345&ref="]
    assert pdf_urls(t["elsevier"], {"doi": "10.1016/j.x"}) == []          # needs a PII
    assert pdf_urls(t["nature"], {"doi": "10.1038/s41598-025-1"}) == ["https://www.nature.com/articles/s41598-025-1.pdf"]
    access = {"idp_entity_id": "https://idp.uni.ac.uk/shibboleth"}
    assert login_url(t["ieee"], access) == ("https://ieeexplore.ieee.org/servlet/wayf.jsp?entityId=https%3A%2F%2Fidp."
                                            "uni.ac.uk%2Fshibboleth&url=https%3A%2F%2Fieeexplore.ieee.org%2FXplore%2Fhome.jsp")
    assert login_url(t["ieee"], {}) == t["ieee"]["login_url"]
    assert via_proxy("https://a.b/c?d=1", {"ezproxy_prefix": "https://ez.uni/login?url="}) == \
        "https://ez.uni/login?url=https%3A%2F%2Fa.b%2Fc%3Fd%3D1"
    assert via_proxy("https://a.b/c", {"openathens_domain": "uni.ac.uk"}) == \
        "https://go.openathens.net/redirector/uni.ac.uk?url=https%3A%2F%2Fa.b%2Fc"
    over = publishers({"ieee": {"pdf_templates": ["X{arnumber}"]}, "new": {"doi_prefixes": ["10.9999"]}})
    assert over["ieee"]["name"] == "IEEE Xplore" and over["ieee"]["pdf_templates"] == ["X{arnumber}"]
    assert publisher_for("10.9999/a", table=over) == "new"


def test_find_pdf_links():
    html = ('<meta name="citation_pdf_url" content="/pdf/1"><iframe src="https://x/stampPDF/getPDF.jsp?a=1&amp;b=2">'
            '<a href="/files/paper.pdf?dl=1">PDF</a><a href="/about">About</a>')
    assert find_pdf_links(html, "https://pub.org/article/1") == [
        "https://pub.org/pdf/1", "https://x/stampPDF/getPDF.jsp?a=1&b=2", "https://pub.org/files/paper.pdf?dl=1"]


def test_open_access_download(publisher_server, tmp_path):
    base, _ = publisher_server
    dl = PaperDownloader({"method": "none"}, http=LocalOnlyHttp())
    res = dl.download(PaperMeta(title="x", pdf_urls=[f"{base}/oa/a.pdf"]), tmp_path / "a.pdf")
    assert res.ok and res.method == "open-access" and is_pdf((tmp_path / "a.pdf").read_bytes())
    res = dl.download(PaperMeta(title="y", doi="10.1109/TNSM.2025.1"), tmp_path / "b.pdf", ids={"arnumber": "1"})
    assert not res.ok and "browser downloads disabled" in res.note and not (tmp_path / "b.pdf").exists()


# ---------------------------------------------------------------- browser session
@needs_browser
def test_browser_login_persists_and_downloads(publisher_server, tmp_path):
    base, server = publisher_server
    profile = tmp_path / "profile"
    with BrowserSession(profile_dir=profile, timeout_seconds=15) as b:
        assert not b.fetch_pdf(f"{base}/pdf/1", tmp_path / "denied.pdf")      # not signed in yet
        # Sign in through the login form (autofill, as `litsuite login publisher --autofill` does).
        b.interactive_login(f"{base}/login?next=/home", input_fn=lambda _="": "", printer=lambda *a: None,
                            username="alice", password="secret")
    # A brand-new browser process re-uses the saved session cookies.
    with BrowserSession(profile_dir=profile, timeout_seconds=15) as b:
        assert "Access provided by" in b.page_text(f"{base}/home")
        assert b.fetch_pdf(f"{base}/pdf/1", tmp_path / "1.pdf")                # direct PDF
        assert b.fetch_pdf(f"{base}/stamp/2", tmp_path / "2.pdf")              # IEEE-style viewer page (iframe)
        assert b.fetch_pdf(f"{base}/article/3", tmp_path / "3.pdf")            # landing page with citation_pdf_url
        assert b._navigate_pdf(f"{base}/download/4", tmp_path / "4.pdf")       # attachment download event
    for i in range(1, 5):
        assert b"PAYWALL" in (tmp_path / f"{i}.pdf").read_bytes()


@needs_browser
def test_login_publishers_and_check(publisher_server, tmp_path, copy_config):
    from litsuite import login
    from litsuite.secrets import set_secret

    base, _ = publisher_server
    path = copy_config(yaml.safe_dump({"access": {"institution": "Example University", "publishers": {
        "testpub": {"name": "Test Publisher", "login_url": f"{base}/login?next=/home", "home": f"{base}/home",
                    "access_markers": ["Access provided by"]}}}}))
    settings = Settings.load(path)
    set_secret(login.SSO_USER, "alice")
    set_secret(login.SSO_PASS, "secret")
    profile = tmp_path / "p2"
    done = login.login_publishers(settings, ["testpub"], autofill=True, input_fn=lambda _="": "",
                                  printer=lambda *a: None, session=BrowserSession(profile_dir=profile, headless=True))
    assert done == ["testpub"]
    ok = login.check_publishers(settings, ["testpub"], printer=lambda *a: None,
                                session=BrowserSession(profile_dir=profile))
    assert ok == {"testpub": True}
    assert any("Publisher browser session" in line for line in login.status_lines(settings))


# ---------------------------------------------------------------- the alerts pipeline
def _meta_fakes(base):
    def by_title(title, author="", year="", http=None, threshold=0.75):
        t = title.lower()
        if t.startswith("hierarchical multi-agent"):
            return PaperMeta(title=title, authors=["Doe", "Smith"], year="2025", venue_full="arXiv",
                             doi="10.48550/arXiv.2510.01234", pub_type="preprint")
        if t.startswith("energy-aware resource allocation"):
            return PaperMeta(title="Energy-Aware Resource Allocation for RAN Slicing With Safe Reinforcement Learning",
                             authors=["Rossi", "Chen"], year="2025", doi="10.1109/TNSM.2025.3561234",
                             venue_full="IEEE Transactions on Network and Service Management", pub_type="journal")
        return None

    def by_pii(pii, http=None):
        if pii == "S1389128626004321":
            return PaperMeta(title="Joint admission control and resource allocation for network slicing using "
                                   "constrained deep reinforcement learning", authors=["Kumar", "Weber"], year="2026",
                             doi="10.1016/j.comnet.2026.112233", venue_full="Computer Networks", pub_type="journal",
                             pdf_urls=[f"{base}/oa/sd.pdf"], abstract="We study admission control. " * 5)
        return None

    def by_arxiv(aid, http=None):
        if aid == "2510.04321":
            return PaperMeta(title="Federated Multi-Agent Reinforcement Learning for Network Slicing in "
                                   "Non-Terrestrial Networks", authors=["Pereira", "Becker"], year="2025",
                             venue_full="arXiv", pub_type="preprint", arxiv_id=aid, pdf_urls=[f"{base}/oa/arxiv.pdf"])
        return None

    return {"crossref_search": by_title, "s2_search": lambda *a, **k: None, "crossref_by_pii": by_pii,
            "arxiv_by_id": by_arxiv, "s2_by_id": lambda *a, **k: None, "crossref_by_doi": lambda *a, **k: None,
            "complete": lambda meta, http=None: meta}


@pytest.fixture
def alert_project(copy_config, library, emails_dir, publisher_server, tmp_path, monkeypatch):
    base, _ = publisher_server
    for name, fn in _meta_fakes(base).items():
        monkeypatch.setattr(alerts, name, fn)
    path = copy_config(yaml.safe_dump({
        "user": {"email": ""},
        "projects": {"slicing": {
            "library": str(library), "recursive": True, "exclude_folders": ["_old"],
            "abbreviations": ["config/abbreviations/network_slicing.yaml"],
            "venues": ["config/venues/common.yaml"], "venue_doi_styles": {"10.1016": "j{code}"},
            "keywords": {"focus": ["network slicing", "resource allocation", "multi-agent reinforcement learning"],
                         "secondary": ["O-RAN", "admission control", "safe reinforcement learning", "6G",
                                       "digital twin", "graph neural network"],
                         "exclude": ["protein", "galaxy"]},
            "alerts": {"accounts": ["saved"], "since_days": 3650, "mode": "auto", "auto_download_score": 4.0,
                       "download_folder": "_new"}}},
        "email_accounts": {"saved": {"provider": "folder", "path": str(emails_dir)}},
        "access": {"method": "browser", "publishers": {"ieee": {"pdf_templates": [f"{base}/download/{{arnumber}}"]}}},
    }))
    settings = Settings.load(path)
    project = settings.project("slicing")
    update_catalog(project.catalog_path, scan_libraries([LocalStorage(library)], True, exclude=["_old"]),
                   enrich=False, backups_dir=project.backups_dir)
    return settings, project


def test_alerts_pipeline_end_to_end(alert_project, library, tmp_path):
    settings, project = alert_project
    lines = []
    res = run_alerts(project, settings, use_browser=False, http=LocalOnlyHttp(), printer=lines.append)
    by_title = {d.candidate.title: d for d in res["decisions"]}

    # Already in the library (filename uses 'NS', the alert says 'Network Slicing').
    assert by_title["DRL-Based Latency-Aware Network Slicing in O-RAN with Time-Varying SLAs"].status == "in_library"
    # Irrelevant papers are never downloaded.
    for t in ("Deep learning for protein structure prediction in plant genomics",
              "A survey on blockchain for supply chain finance and logistics", "Bayesian Inference for Galaxy Cluster Masses"):
        assert by_title[t].status == "low_score"
    # Open-access PDFs are downloaded, renamed with the convention + abbreviations, saved under _new/.
    expected = {
        "Hierarchical Multi-Agent Deep Reinforcement Learning for End-to-End Network Slicing in O-RAN":
            "PREP 25 Doe - Hierarchical_MA-DRL_for_E2E_NS_in_O-RAN.pdf",
        "Joint admission control and resource allocation for network slicing using constrained deep reinforcement learning":
            "jcomnet 26 Kumar - Joint_admission_control_and_RA_for_NS_using_constrained_DRL.pdf",
        "Federated Multi-Agent Reinforcement Learning for Network Slicing in Non-Terrestrial Networks":
            "PREP 25 Pereira - Federated_MARL_for_NS_in_Non-Terrestrial_Networks.pdf",
    }
    for title, fname in expected.items():
        d = by_title[title]
        assert (d.status, d.filename, d.method) == ("downloaded", fname, "open-access"), title
        assert (library / "_new" / fname).exists()
    # Paywalled IEEE papers without a browser: reported with links and a suggested name, retried next run.
    ieee = by_title["Energy-Aware Resource Allocation for RAN Slicing with Safe Reinforcement Learning"]
    assert ieee.status == "needs_manual" and ieee.filename == \
        "TNSM 25 Rossi - Energy-Aware_RA_for_RAN_Slicing_With_Safe_RL.pdf"
    assert any("ieeexplore.ieee.org" in link or "doi.org" in link for link in ieee.links)
    gnn = by_title["Graph Neural Network-Based Admission Control for 6G Network Slicing"]
    # No metadata found (offline): venue unknown, year taken from the alert email.
    assert (gnn.status, gnn.filename) == ("needs_manual", "MISC 26 - Graph_Neural_Network-Based_Admission_Control_for_6G_NS.pdf")
    manual = sorted(d.candidate.title for d in res["decisions"] if d.status == "needs_manual")
    assert len(manual) == 3 and res["counts"] == {"downloaded": 3, "needs_manual": 3, "low_score": 4, "in_library": 1}

    # Catalog rows were added for the downloads.
    rows, _ = read_catalog(project.catalog_path)
    new = [r for r in rows if r.get("source", "").startswith("alert:")]
    assert len(new) == 3 and all(r["relative_path"].startswith("_new/") for r in new)
    sd = [r for r in new if r["venue_acronym"] == "jcomnet"][0]
    assert sd["doi"] == "10.1016/j.comnet.2026.112233" and sd["reference_key"] == "jcomnet26kumar"
    # A later `catalog update` sees the new files as already cataloged.
    upd = update_catalog(project.catalog_path, scan_libraries([LocalStorage(library)], True, exclude=["_old"]),
                         enrich=False, backups_dir=project.backups_dir)
    assert upd["added"] == [] and upd["renamed"] == []

    # Audit trail.
    report = open(res["report"], encoding="utf-8").read()
    assert "## Downloaded (3)" in report and "## Needs manual download" in report
    log_rows = list(csv.DictReader(open(project.data_dir / "downloads_log.csv", encoding="utf-8-sig")))
    assert sum(1 for r in log_rows if r["status"] == "downloaded") == 3

    # Second run: emails were consumed; only the failed papers are retried.
    res2 = run_alerts(project, settings, use_browser=False, http=LocalOnlyHttp(), printer=lines.append)
    assert sorted(d.candidate.title for d in res2["decisions"]) == manual
    assert all(d.status == "needs_manual" for d in res2["decisions"])
    state = json.loads((project.state_dir / "alerts_state.json").read_text())
    assert state["decisions"][ieee.candidate.key]["attempts"] == 2


def test_report_mode_is_a_dry_run(alert_project, library):
    settings, project = alert_project
    res = run_alerts(project, settings, mode="report", http=LocalOnlyHttp(), printer=lambda *a: None)
    assert res["counts"].get("report_only", 0) >= 3 and "downloaded" not in res["counts"]
    assert not (library / "_new").exists()
    assert not (project.state_dir / "alerts_state.json").exists()   # nothing used up
    res = run_alerts(project, settings, mode="auto", use_browser=False, http=LocalOnlyHttp(), printer=lambda *a: None)
    assert res["counts"]["downloaded"] == 3


def test_ask_mode_lets_the_user_choose(alert_project, library):
    settings, project = alert_project
    shown = []
    answers = iter(["1"])
    res = run_alerts(project, settings, mode="ask", use_browser=False, http=LocalOnlyHttp(),
                     input_fn=lambda prompt="": next(answers), printer=shown.append)
    assert res["counts"]["downloaded"] == 1 and res["counts"]["skipped"] == 5
    assert any(line.startswith("*  1.") for line in shown)            # pre-selected by score
    assert parse_selection("1, 3-4 9", 5) == [0, 2, 3]


def test_read_emails_are_never_opened(alert_project, emails_dir):
    settings, project = alert_project
    for f in emails_dir.glob("*.eml"):                                 # the user has read them all
        f.write_bytes(b"Status: RO\n" + f.read_bytes())
    res = run_alerts(project, settings, use_browser=False, http=LocalOnlyHttp(), printer=lambda *a: None)
    assert res["decisions"] == [] and res["counts"] == {}


def test_mark_as_read_only_when_enabled(alert_project, monkeypatch):
    settings, project = alert_project
    marked = []

    class Box:
        def __init__(self, cfg):
            self.cfg = cfg

        def search(self, senders, since, limit=300):
            from litsuite.mail.folder import FolderMailbox

            yield from FolderMailbox(self.cfg).search(senders, since)

        def mark_read(self, msgs):
            marked.extend(m.id for m in msgs)
            return len(msgs)

        def close(self):
            pass

    run_alerts(project, settings, mode="auto", use_browser=False, http=LocalOnlyHttp(),
               mailbox_factory=Box, printer=lambda *a: None)
    assert marked == []                                                # default: read state untouched
    settings.data["email_accounts"]["saved"]["mark_as_read"] = True
    run_alerts(project, settings, mode="auto", use_browser=False, http=LocalOnlyHttp(), reprocess=True,
               mailbox_factory=Box, printer=lambda *a: None)
    assert len(marked) == 5                                            # the 5 alert emails, not the personal one


@needs_browser
def test_alerts_pipeline_with_signed_in_browser(alert_project, publisher_server, tmp_path, library):
    """The paywalled IEEE paper is fetched through the signed-in browser session."""
    settings, project = alert_project
    base, _ = publisher_server
    from litsuite import config

    profile = config.APP_HOME / "browser" / "default"
    with BrowserSession(profile_dir=profile) as b:
        b.interactive_login(f"{base}/login?next=/home", input_fn=lambda _="": "", printer=lambda *a: None,
                            username="alice", password="secret")
    res = run_alerts(project, settings, http=LocalOnlyHttp(), printer=lambda *a: None)
    ieee = [d for d in res["decisions"] if d.candidate.ieee_arnumber == "11012345"][0]
    assert (ieee.status, ieee.method) == ("downloaded", "browser")
    pdf = library / "_new" / "TNSM 25 Rossi - Energy-Aware_RA_for_RAN_Slicing_With_Safe_RL.pdf"
    assert b"PAYWALL" in pdf.read_bytes()


def test_library_matcher_canonical_titles():
    from litsuite.abbreviations import Abbreviator

    m = LibraryMatcher([{"filename": "ICNC 24 R - DRL-Based_Latency-Aware_NS_in_O-RAN.pdf", "title_official": "",
                         "doi": "10.1/abc"}], Abbreviator({"Network Slicing": "NS"}))
    assert m.find("DRL-Based Latency-Aware Network Slicing in O-RAN") is not None
    assert m.find("Totally different", "10.1/ABC") is not None
    assert m.find("Latency-Aware Edge Caching in Vehicular Networks") is None


def test_alert_state_roundtrip(tmp_path):
    st = AlertState(tmp_path / "s.json")
    st.processed["m1"] = "2026-10-01"
    st.processed["old"] = "2020-01-01"
    st.save()
    st2 = AlertState(tmp_path / "s.json")
    assert "m1" in st2.processed and "old" not in st2.processed


def test_browser_launch_failure_is_reported_once(tmp_path):
    attempts = []

    class Broken:
        def __enter__(self):
            attempts.append(1)
            raise RuntimeError("Could not start the browser. python -m playwright install chromium")

    dl = PaperDownloader({"method": "browser"}, http=LocalOnlyHttp(), browser_factory=Broken)
    for i in range(3):
        res = dl.download(PaperMeta(title="t", doi="10.1109/TNSM.2025.1"), tmp_path / f"{i}.pdf", ids={"arnumber": "1"})
        assert not res.ok and "playwright install chromium" in res.note
    assert attempts == [1]


def test_no_report_without_new_alerts(alert_project, emails_dir):
    settings, project = alert_project
    for f in emails_dir.glob("*.eml"):
        f.unlink()
    res = run_alerts(project, settings, use_browser=False, http=LocalOnlyHttp(), printer=lambda *a: None)
    assert res["report"] == "" and res["counts"] == {}
    assert not project.reports_dir.exists() or not list(project.reports_dir.glob("alerts-*"))
