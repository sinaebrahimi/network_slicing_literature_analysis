"""Every CLI command and sign-in path not exercised elsewhere, through real code with
only the network replaced (responses) or the interactive parts answered."""

import datetime as dt
import json
import subprocess
import sys

import pytest
import responses
import yaml

from conftest import FAKE_PDF, FIXTURES

import litsuite.http as lhttp
from litsuite.catalog import read_catalog
from litsuite.cli import main
from litsuite.config import Settings
from test_cloud_mail_clients_real import _mock_entra
from test_metadata_real_http import ABS, ARXIV_ATOM, CR_ITEM, IEEE_PAGE


@pytest.fixture(autouse=True)
def fast_http(monkeypatch):
    monkeypatch.setattr(lhttp, "_default", lhttp.Http("lab@example.org", {k: 0 for k in lhttp.HOST_DELAYS}))
    monkeypatch.setattr(lhttp.time, "sleep", lambda s: None)


def answers(*items):
    it = iter(items)
    return lambda prompt="": next(it)


@pytest.fixture
def project_cfg(copy_config, library):
    path = copy_config(yaml.safe_dump({
        "user": {"email": "lab@example.org"},
        "projects": {"slicing": {"library": str(library), "recursive": True, "exclude_folders": ["_old"],
                                 "abbreviations": ["config/abbreviations/network_slicing.yaml"],
                                 "keywords": {"focus": ["network slicing"]}}},
        "email_accounts": {
            "saved": {"provider": "folder", "path": str(FIXTURES / "emails")},
            "uni": {"provider": "outlook", "client_id": "cid", "tenant": "organizations"},
            "g": {"provider": "gmail", "client_secret_file": "/x.json", "mark_as_read": True},
            "imapacct": {"provider": "imap", "host": "imap.example.org", "username": "me"}},
        "cloud": {"onedrive": {"client_id": "cid", "tenant": "organizations"}, "gdrive": {"client_secret_file": "/x.json"}},
    }))
    return path


# ---------------------------------------------------------------- download command
@responses.activate
def test_download_by_doi_url_and_arxiv(project_cfg, library, capsys):
    cr = dict(CR_ITEM, link=[{"URL": "https://oa.example.org/liu.pdf", "content-type": "application/pdf",
                              "intended-application": "similarity-checking"}])
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234", json={"message": cr})
    responses.add(responses.GET, "https://oa.example.org/liu.pdf", body=FAKE_PDF, content_type="application/pdf")
    responses.add(responses.GET, "https://api.unpaywall.org/v2/10.1109/TNSM.2024.3361234", status=404)
    rc = main(["--config", str(project_cfg), "download", "-p", "slicing", "--doi", "10.1109/TNSM.2024.3361234"])
    out = capsys.readouterr().out
    name = "TNSM 24 Liu - MA-DRL_for_E2E_NS.pdf"
    assert rc == 0 and f"downloaded: {name}" in out and (library / name).read_bytes() == FAKE_PDF
    prj = Settings.load(project_cfg).project("slicing")
    row = [r for r in read_catalog(prj.catalog_path)[0] if r["filename"] == name][0]
    assert row["doi"] == "10.1109/TNSM.2024.3361234" and row["source"] == "alert:manual" and row["abstract"] == ABS
    # The same paper again: recognised as already in the library.
    assert main(["--config", str(project_cfg), "download", "-p", "slicing", "--doi", "10.1109/TNSM.2024.3361234"]) == 2
    assert "already in library" in capsys.readouterr().out

    # An IEEE Xplore link (no DOI in the URL): the DOI is read from the page itself.
    page = IEEE_PAGE.replace('"doi":"10.1109/X"', '"doi":"10.1109/MNET.2025.7"')
    responses.add(responses.GET, "https://ieeexplore.ieee.org/document/11012345", body=page)
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/MNET.2025.7", json={"message": dict(
        CR_ITEM, DOI="10.1109/MNET.2025.7", title=["Safe Reinforcement Learning for RAN Slicing"],
        author=[{"family": "Rossi"}], **{"container-title": ["IEEE Network"], "link": []})})
    responses.add(responses.GET, "https://api.unpaywall.org/v2/10.1109/MNET.2025.7", status=404)
    rc = main(["--config", str(project_cfg), "download", "-p", "slicing", "--no-browser",
               "--url", "https://ieeexplore.ieee.org/document/11012345"])
    out = capsys.readouterr().out
    assert rc == 2 and "needs_manual: MNET 24 Rossi - Safe_RL_for_RAN_Slicing.pdf" in out
    assert "Open manually: https://ieeexplore.ieee.org/document/11012345  https://doi.org/10.1109/MNET.2025.7" in out

    # arXiv: open-access PDF from arxiv.org.
    responses.add(responses.GET, "https://export.arxiv.org/api/query", body=ARXIV_ATOM)
    responses.add(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/ARXIV:2503.04184", status=404)
    responses.add(responses.GET, "https://arxiv.org/pdf/2503.04184", body=FAKE_PDF, content_type="application/pdf")
    assert main(["--config", str(project_cfg), "download", "-p", "slicing", "--arxiv", "2503.04184"]) == 0
    assert (library / "PREP 25 Shahid - Large-Scale_AI_in_Telecom_Charting_the_Roadmap_for_Innovation.pdf").exists()

    # Nothing identifies the paper: reported, nothing saved under a meaningless name.
    responses.add(responses.GET, "https://example.org/some/page", body="<html>nothing here</html>")
    before = sorted(p.name for p in library.iterdir())
    assert main(["--config", str(project_cfg), "download", "-p", "slicing", "--no-browser",
                 "--url", "https://example.org/some/page"]) == 2
    assert "could not identify this paper" in capsys.readouterr().out
    assert sorted(p.name for p in library.iterdir()) == before


# ---------------------------------------------------------------- login sub-commands
@responses.activate
def test_login_subcommands(project_cfg, monkeypatch, capsys):
    import litsuite.login as login
    from litsuite.secrets import get_secret

    cfg = str(project_cfg)
    # sso: username typed, password hidden
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret")
    assert main(["--config", cfg, "login", "sso"], input_fn=answers("alice@uni.ac.uk")) == 0
    assert get_secret("sso:username") == "alice@uni.ac.uk" and get_secret("sso:password") == "s3cret"
    # email: folder (nothing to do), outlook (real MSAL device flow), gmail (modify scope when mark_as_read)
    assert main(["--config", cfg, "login", "email", "saved"]) == 0
    assert "Nothing to sign in to" in capsys.readouterr().out
    _mock_entra("organizations")
    assert main(["--config", cfg, "login", "email", "uni"]) == 0
    out = capsys.readouterr().out
    assert "ABCD-EFGH" in out and "Outlook account 'uni' connected." in out
    seen = {}
    monkeypatch.setattr("litsuite.mail.gmail.google_credentials",
                        lambda secret, scopes, cache, interactive=True: seen.update(scopes=scopes, cache=cache))
    assert main(["--config", cfg, "login", "email", "g"]) == 0
    assert seen == {"scopes": ["https://www.googleapis.com/auth/gmail.modify"], "cache": "gmail-g-modify"}
    # cloud: onedrive (real MSAL), gdrive (Drive scope), rclone (instructions)
    assert main(["--config", cfg, "login", "cloud", "onedrive"]) == 0
    monkeypatch.setattr("litsuite.storage.gdrive.google_credentials",
                        lambda secret, scopes, cache, interactive=True: seen.update(scopes=scopes, cache=cache))
    assert main(["--config", cfg, "login", "cloud", "gdrive"]) == 0 and seen["cache"] == "gdrive"
    assert main(["--config", cfg, "login", "cloud", "rclone"]) == 0
    assert "rclone config" in capsys.readouterr().out
    assert main(["--config", cfg, "login", "cloud", "box"]) == 2
    # publisher / check / all: the browser parts are covered in test_downloads_and_alerts / test_e2e_real_tools
    calls = []
    monkeypatch.setattr(login, "login_publishers", lambda s, names, autofill, input_fn=input: calls.append(
        ("pub", names, autofill)) or names)
    monkeypatch.setattr(login, "check_publishers", lambda s, names=None, **k: calls.append(("check", names)) or {})
    monkeypatch.setattr(login, "login_email", lambda s, name, input_fn=input: calls.append(("email", name)) or "ok")
    assert main(["--config", cfg, "login", "publisher", "ieee,elsevier", "--autofill"]) == 0
    assert main(["--config", cfg, "login", "check", "ieee"]) == 0
    # all: asks per account in settings order (g, imapacct, uni; folder accounts skipped), then publishers
    assert main(["--config", cfg, "login", "all"], input_fn=answers("y", "n", "y", "y")) == 0
    assert calls == [("pub", ["ieee", "elsevier"], True), ("check", ["ieee"]), ("email", "g"), ("email", "uni"),
                     ("pub", ["all"], False), ("check", None)]
    assert main(["--config", cfg, "login"]) == 0
    status = capsys.readouterr().out
    assert "uni                  outlook  ready" in status and "University SSO autofill: stored" in status
    assert "onedrive  connected" in status


@responses.activate
def test_open_mailbox_from_settings_for_gmail_and_outlook(project_cfg):
    from litsuite.mail import open_mailbox
    from litsuite.mail.gmail import GmailMailbox
    from litsuite.mail.outlook import OutlookMailbox
    from litsuite.oauth import microsoft_token
    from litsuite.secrets import token_path

    settings = Settings.load(project_cfg)
    _mock_entra("organizations")
    microsoft_token("cid", ["Mail.Read"], "mail-uni", "organizations", printer=lambda *a: None)
    assert isinstance(open_mailbox(settings.email_account("uni")), OutlookMailbox)
    token_path("google", "gmail-g-modify").write_text(json.dumps({
        "token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "expiry": "2099-01-01T00:00:00Z"}))
    assert isinstance(open_mailbox(settings.email_account("g")), GmailMailbox)   # real discovery-based client
    with pytest.raises(ValueError):
        open_mailbox({"name": "x", "provider": "pigeon"})


# ---------------------------------------------------------------- doctor
@responses.activate
def test_doctor_online(project_cfg, capsys, copy_config, tmp_path):
    from litsuite.doctor import ENDPOINTS

    for _, url in ENDPOINTS:
        responses.add(responses.GET, url.split("?")[0], json={})
    assert main(["--config", str(project_cfg), "doctor", "--online"]) == 0
    out = capsys.readouterr().out
    assert "All good." in out and "[slicing] library" in out and "[x] CrossRef" in out
    bad = copy_config(yaml.safe_dump({"projects": {
        "a": {"library": str(tmp_path / "missing")}, "b": {"library": "dropbox:/x"}}}))
    assert main(["--config", str(bad), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "NOT FOUND" in out and "dropbox:/x (cloud API" in out


# ---------------------------------------------------------------- remaining pieces
def test_plain_text_publisher_alert():
    from litsuite.mail.base import MailMessage
    from litsuite.mail.parsers import all_sources, parse_message

    text = ("New content alert: Transactions on Emerging Telecommunications Technologies\n\n"
            "Federated Learning for Network Slicing in Vehicular Networks\n"
            "https://onlinelibrary.wiley.com/doi/10.1002/ett.70001?af=R\n\n"
            "Manage your alerts: https://onlinelibrary.wiley.com/action/alerts\n")
    msg = MailMessage(id="w1", subject="New content alert", sender="alerts@wiley.com", text=text)
    cands = parse_message(msg, "wiley", all_sources())
    assert [(c.title, c.doi) for c in cands] == [("Federated Learning for Network Slicing in Vehicular Networks",
                                                  "10.1002/ett.70001")]


@responses.activate
def test_metadata_complete_and_landing_page():
    from litsuite.metadata import PaperMeta, complete, identify_landing_page

    h = lhttp.Http(delays={k: 0 for k in lhttp.HOST_DELAYS})
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234",
                  json={"message": dict(CR_ITEM, abstract="")})
    responses.add(responses.GET, "https://api.openalex.org/works/doi:10.1109/TNSM.2024.3361234", status=404)
    responses.add(responses.GET, "https://doi.org/10.1109/TNSM.2024.3361234", status=302,
                  headers={"Location": "https://ieeexplore.ieee.org/document/1"})
    responses.add(responses.GET, "https://ieeexplore.ieee.org/document/1", body=IEEE_PAGE)
    m = complete(PaperMeta(doi="10.1109/TNSM.2024.3361234"), h)
    assert m.title == CR_ITEM["title"][0] and m.venue_full and m.abstract.startswith("Network slicing enables")
    responses.add(responses.GET, "https://journals.example.org/a/1", body=(
        '<meta name="citation_title" content="A Study of Things"><meta name="citation_doi" content="doi:10.5555/abc.1">'))
    assert identify_landing_page("https://journals.example.org/a/1", h)["doi"] == "10.5555/abc.1"
    responses.add(responses.GET, "https://journals.example.org/a/2", status=404)
    assert identify_landing_page("https://journals.example.org/a/2", h) == {}


def test_setup_wizard_branches(tmp_path, library, monkeypatch):
    from litsuite import setup_wizard

    monkeypatch.setattr("litsuite.storage.sync_folders.detect_all", lambda: {"onedrive": [tmp_path]})
    out = []
    existing = tmp_path / "config" / "settings.yaml"
    existing.parent.mkdir()
    existing.write_text("projects: {}\n")
    setup_wizard.run(Settings({}), answers("bio", str(tmp_path / "nowhere"), "a, b", "", "", "", "n"),
                     printer=out.append, target=existing)
    text = "\n".join(out)
    assert "folder not found" in text and "already exists" in text and "bio:" in text
    assert existing.read_text() == "projects: {}\n"                 # an existing file is never overwritten
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "A 24 - x.pdf").write_bytes(b"%PDF")
    target = tmp_path / "new" / "settings.yaml"
    setup_wizard.run(Settings({}), answers("", str(flat), "", "", "", "", ""), printer=out.append, target=target)
    prj = Settings.load(target).project("my_research")
    assert prj.recursive_setting is False and prj.library_specs == [str(flat)]


def test_dashboard_errors(copy_config, tmp_path):
    from litsuite.config import ConfigError
    from litsuite.dashboard import load_dashboard

    path = copy_config(yaml.safe_dump({"projects": {"empty": {"data_dir": str(tmp_path / "none")}}}))
    with pytest.raises(FileNotFoundError):
        load_dashboard("empty", path)
    with pytest.raises(ConfigError):
        load_dashboard("not_a_project", path)


def test_python_dash_m(tmp_path):
    p = subprocess.run([sys.executable, "-m", "litsuite", "--version"], capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 0 and "litsuite 2." in p.stdout


def test_alert_date_window_and_limits(copy_config, library, tmp_path):
    """since_days and max_downloads are respected."""
    from litsuite.alerts import collect_candidates

    old = tmp_path / "old"
    old.mkdir()
    raw = (FIXTURES / "emails" / "scholar_alert.eml").read_bytes()
    (old / "a.eml").write_bytes(raw)
    path = copy_config(yaml.safe_dump({"projects": {"p": {"library": str(library), "alerts": {"accounts": ["s"]}}},
                                       "email_accounts": {"s": {"provider": "folder", "path": str(old)}}}))
    settings = Settings.load(path)
    prj = settings.project("p")
    days = (dt.date.today() - dt.date(2026, 10, 12)).days
    cands, ids, _ = collect_candidates(prj, settings, since_days=days + 1, printer=lambda *a: None)
    assert len(cands) == 4 and ids == ["<scholar-0001@google.com>"]
    cands, ids, _ = collect_candidates(prj, settings, since_days=max(days - 1, 0), printer=lambda *a: None)
    assert cands == [] and ids == []
