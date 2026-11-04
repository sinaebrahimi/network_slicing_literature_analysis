"""End-to-end checks with real tools: the rclone binary, Chromium sign-in variants
(Microsoft two-step, EZproxy, JavaScript PDF redirect), the dashboard notebook,
the installed `litsuite` command run as a subprocess, and the shell scripts."""

import os
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import FAKE_PDF, ROOT, chromium_available

from litsuite.catalog import read_catalog

needs_browser = pytest.mark.skipif(not chromium_available(), reason="Playwright Chromium not available")
LITSUITE = shutil.which("litsuite")


# ---------------------------------------------------------------- rclone (real binary)
@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
@pytest.mark.parametrize("style", ["on-the-fly", "configured-remote"])
def test_rclone_real_binary(tmp_path, monkeypatch, style):
    from litsuite.storage import open_storage

    remote_dir = tmp_path / "cloud" / "Research Lit"
    (remote_dir / "Surveys").mkdir(parents=True)
    (remote_dir / "A 24 - T.pdf").write_bytes(FAKE_PDF)
    (remote_dir / "Surveys" / "B 23 - S.pdf").write_bytes(FAKE_PDF)
    (remote_dir / "notes.txt").write_text("x")
    if style == "on-the-fly":
        spec = f"rclone::local:{remote_dir}"
    else:   # a remote defined like `rclone config` would, via rclone's environment configuration
        monkeypatch.setenv("RCLONE_CONFIG_MYCLOUD_TYPE", "local")
        spec = f"rclone:mycloud:{remote_dir}"
    st = open_storage(spec)
    assert [f.relative_path for f in st.list_files()] == ["A 24 - T.pdf", "Surveys/B 23 - S.pdf"]
    assert [f.relative_path for f in st.list_files(recursive=False)] == ["A 24 - T.pdf"]
    src = tmp_path / "new.pdf"
    src.write_bytes(FAKE_PDF)
    st.save(src, "_new/C 25 - New paper.pdf")
    assert (remote_dir / "_new" / "C 25 - New paper.pdf").read_bytes() == FAKE_PDF
    st.rename("_new/C 25 - New paper.pdf", "C 25 - New paper.pdf")
    assert (remote_dir / "C 25 - New paper.pdf").exists() and not (remote_dir / "_new" / "C 25 - New paper.pdf").exists()
    with pytest.raises(RuntimeError):
        open_storage("rclone:nosuchremote:x").list_files()


# ---------------------------------------------------------------- browser sign-in variants
@needs_browser
def test_microsoft_two_step_autofill_and_js_pdf_redirect(publisher_server, tmp_path, copy_config):
    from litsuite import login
    from litsuite.config import Settings
    from litsuite.fetch.browser import BrowserSession
    from litsuite.secrets import set_secret

    base, _ = publisher_server
    path = copy_config(yaml.safe_dump({"access": {"publishers": {"uni": {
        "name": "Uni SSO", "login_url": f"{base}/ms/login", "home": f"{base}/home",
        "access_markers": ["Access provided by"]}}}}))
    settings = Settings.load(path)
    set_secret(login.SSO_USER, "alice@uni.ac.uk")
    set_secret(login.SSO_PASS, "secret")
    profile = tmp_path / "prof"
    login.login_publishers(settings, ["uni"], autofill=True, input_fn=lambda _="": "", printer=lambda *a: None,
                           session=BrowserSession(profile_dir=profile))
    assert login.check_publishers(settings, ["uni"], printer=lambda *a: None,
                                  session=BrowserSession(profile_dir=profile)) == {"uni": True}
    with BrowserSession(profile_dir=profile, timeout_seconds=20) as b:
        assert b.fetch_pdf(f"{base}/jsredir/7", tmp_path / "js.pdf")       # JS redirect followed in a real tab
    assert b"PAYWALL" in (tmp_path / "js.pdf").read_bytes()


@needs_browser
def test_ezproxy_single_sign_in_then_downloads(publisher_server, tmp_path, copy_config, monkeypatch, virtual_display):
    """Headed sign-in window (as a user sees it) on a virtual screen, then headless downloads."""
    from litsuite import config, login
    from litsuite.config import Settings
    from litsuite.fetch.downloader import PaperDownloader
    from litsuite.metadata import PaperMeta
    from litsuite.secrets import set_secret
    from test_downloads_and_alerts import LocalOnlyHttp

    base, _ = publisher_server
    access = {"ezproxy_prefix": f"{base}/ezproxy/login?url=", "institution": "Example University",
              "publishers": {"ieee": {"home": f"{base}/proxied/pdf/home",
                                      "pdf_templates": [f"{base}/proxied/pdf/{{arnumber}}"]}}}
    settings = Settings.load(copy_config(yaml.safe_dump({"access": access})))
    set_secret(login.SSO_USER, "alice")
    set_secret(login.SSO_PASS, "secret")
    # Without a screen, the sign-in fails with a clear explanation.
    monkeypatch.delenv("DISPLAY")
    with pytest.raises(RuntimeError, match="computer with a screen"):
        login.login_publishers(settings, ["ieee"], input_fn=lambda _="": "", printer=lambda *a: None)
    monkeypatch.setenv("DISPLAY", virtual_display)
    # With a library proxy, one sign-in covers every publisher.
    assert login.login_publishers(settings, ["ieee", "elsevier"], autofill=True, input_fn=lambda _="": "",
                                  printer=lambda *a: None) == ["proxy"]
    assert (config.APP_HOME / "browser" / "default").exists()
    dl = PaperDownloader(settings.access, http=LocalOnlyHttp())
    try:
        res = dl.download(PaperMeta(title="t", doi="10.1109/TNSM.2025.9"), tmp_path / "p.pdf", ids={"arnumber": "99"})
    finally:
        dl.close()
    assert res.ok and res.method == "browser" and res.url.startswith(f"{base}/ezproxy/login?url=")
    assert b"EZPROXY" in (tmp_path / "p.pdf").read_bytes()


# ---------------------------------------------------------------- dashboard notebook
def test_dashboard_notebook_executes(tmp_path):
    nbformat = pytest.importorskip("nbformat")
    pytest.importorskip("nbclient")
    from nbclient import NotebookClient

    work = tmp_path / "ws"
    (work / "config").mkdir(parents=True)
    shutil.copytree(ROOT / "config" / "dashboard", work / "config" / "dashboard")
    shutil.copytree(ROOT / "config" / "abbreviations", work / "config" / "abbreviations")
    data = work / "projects" / "network_slicing"
    data.mkdir(parents=True)
    shutil.copy(ROOT / "projects" / "network_slicing" / "litreview.csv", data / "litreview.csv")
    (work / "config" / "settings.yaml").write_text(yaml.safe_dump({"projects": {"network_slicing": {
        "data_dir": "projects/network_slicing", "dashboard": {"file": "config/dashboard/network_slicing.yaml"},
        "keywords": {"focus": ["network slicing"]}}}}))
    nb = nbformat.read(ROOT / "litreview_dashboard.ipynb", as_version=4)
    NotebookClient(nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(work)}}).execute()
    out = "".join(o.get("text", "") for c in nb.cells for o in c.get("outputs", []) if o.get("output_type") == "stream")
    assert "Loaded 706 papers" in out and "Total papers:            706" in out
    errors = [o for c in nb.cells for o in c.get("outputs", []) if o.get("output_type") == "error"]
    assert errors == []
    figs = sorted(p.name for p in (data / "figures").glob("*.png"))
    assert {"publisher_dist.png", "timeline.png", "topic_heatmap.png", "top_terms.png", "wordclouds.png"} <= set(figs)


# ---------------------------------------------------------------- the installed command (subprocess)
def run(args, cwd, env, stdin=""):
    p = subprocess.run([LITSUITE, *args], cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


@pytest.fixture
def cli_env(tmp_path, library):
    env = {**os.environ, "LITSUITE_HOME": str(tmp_path / "home"), "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    for sub in ("abbreviations", "venues", "dashboard"):
        shutil.copytree(ROOT / "config" / sub, ws / "config" / sub)
    return env, ws


@pytest.mark.skipif(not LITSUITE, reason="litsuite is not installed (pip install -e .)")
def test_installed_command_end_to_end(cli_env, library, emails_dir, tmp_path):
    env, ws = cli_env
    rc, out = run(["--version"], ws, env)
    assert rc == 0 and "litsuite 2." in out
    rc, out = run(["--help"], ws, env)
    assert rc == 0 and "catalog" in out and "alerts" in out
    # 1. Ad-hoc catalog: folder and subfolder choice typed on stdin (no settings file yet).
    rc, out = run(["catalog", "create", "--output", str(tmp_path / "adhoc.csv"), "--offline"], ws, env,
                  stdin=f"{library}\ns\ny\nn\n")
    assert rc == 0, out
    assert "Surveys  (1 PDFs)" in out and "Added: 4" in out
    assert {r["subfolder"] for r in read_catalog(tmp_path / "adhoc.csv")[0]} == {"", "Surveys"}
    # 2. Guided setup writes config/settings.yaml.
    rc, out = run(["init"], ws, env, stdin=f"slicing\n{library}\nyes\nnetwork slicing, resource allocation\n"
                                          "O-RAN, admission control\nprotein\n\nn\n")
    assert rc == 0 and "Created" in out, out
    cfg = yaml.safe_load((ws / "config" / "settings.yaml").read_text())
    cfg["projects"]["slicing"].update({"abbreviations": ["config/abbreviations/network_slicing.yaml"],
                                       "exclude_folders": ["_old"]})
    cfg["projects"]["slicing"]["alerts"].update({"accounts": ["saved"], "since_days": 3650})
    cfg["email_accounts"] = {"saved": {"provider": "folder", "path": str(emails_dir)}}
    (ws / "config" / "settings.yaml").write_text(yaml.safe_dump(cfg))
    # 3. Project commands.
    rc, out = run(["catalog", "update", "-p", "slicing", "--offline"], ws, env)
    assert rc == 0 and "Added: 4" in out, out
    (library / "MNET 25 Fryganiotis - Model_Predictive_Control-Based_Dynamic_RA_Enabler_for_Resilient_NS.pdf").write_bytes(FAKE_PDF)
    rc, out = run(["catalog", "update", "-p", "slicing", "--offline"], ws, env)
    assert rc == 0 and "Added: 1" in out
    for args, expect in [
        (["projects"], "[slicing]"),
        (["catalog", "duplicates", "-p", "slicing"], "group(s) of possible duplicates"),
        (["abbrev", "list", "-p", "slicing"], "NS         <- Network Slicing"),
        (["abbrev", "test", "-p", "slicing", "Deep Reinforcement Learning for Network Slicing"], "DRL_for_NS"),
        (["abbrev", "learn", "-p", "slicing", "--min-count", "1"], "abbreviations"),
        (["keywords", "suggest", "-p", "slicing", "--save", "--top", "10"], "Saved to"),
        (["keywords", "show", "-p", "slicing"], "focus:     ['network slicing', 'resource allocation']"),
        (["keywords", "test", "-p", "slicing", "Resource allocation for network slicing in O-RAN"], "focus:network slicing"),
        (["login", "status"], "saved                folder   ready"),
        (["where"], ""),
        (["doctor"], "Optional components"),
        (["rename", "-p", "slicing"], "rename-plan-"),
        (["alerts", "-p", "slicing", "--mode", "report"], "Report:"),
    ]:
        rc, out = run(args, ws, env)
        assert rc in (0, 1) and expect in out, (args, out)
    report = sorted((ws / "projects" / "slicing" / "reports").glob("alerts-*.md"))[-1].read_text()
    assert "Relevant (report mode, not downloaded)" in report
    # 4. Interactive menu (no command): choose "Update a catalog".
    rc, out = run(["--config", str(ws / "config" / "settings.yaml")], ws, env, stdin="1\n")
    assert rc == 0 and "what would you like to do?" in out
    # 5. Errors are reported cleanly, never as tracebacks.
    rc, out = run(["catalog", "update", "-p", "nope"], ws, env)
    assert rc == 2 and "Unknown project 'nope'" in out and "Traceback" not in out
    rc, out = run(["login", "email"], ws, env)
    assert rc == 2 and "Which account?" in out
    rc, out = run(["login", "cloud", "dropbox"], ws, env)
    assert rc == 2 and "app_key" in out and "Traceback" not in out


@pytest.mark.skipif(not LITSUITE, reason="litsuite is not installed")
def test_v1_wrapper_scripts(library, tmp_path):
    env = {**os.environ, "LITSUITE_HOME": str(tmp_path / "home"), "LITERATURE_DIR": str(library)}
    out_csv = tmp_path / "v1.csv"
    p = subprocess.run([sys.executable, str(ROOT / "literature_catalog.py"), "--output", str(out_csv), "--offline",
                        "--no-recursive"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    rows, _ = read_catalog(out_csv)
    assert len(rows) == 3
    for r in rows:
        r["abstract"] = "already there"
    from litsuite.catalog import write_catalog

    write_catalog(out_csv, rows)
    p = subprocess.run([sys.executable, str(ROOT / "fill_abstracts.py"), "--input", str(out_csv)], cwd=tmp_path,
                       env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "Filled 0/0" in p.stdout, p.stdout + p.stderr


@pytest.mark.skipif(not LITSUITE or not shutil.which("bash"), reason="needs bash and litsuite")
def test_run_alerts_script(cli_env, library, emails_dir, tmp_path):
    env, ws = cli_env
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "run_alerts.sh", repo / "scripts" / "run_alerts.sh")
    (ws / "config" / "settings.yaml").write_text(yaml.safe_dump({
        "projects": {"p": {"library": str(library), "keywords": {"focus": ["network slicing"]},
                           "alerts": {"accounts": ["saved"], "since_days": 3650}}},
        "email_accounts": {"saved": {"provider": "folder", "path": str(emails_dir)}}}))
    p = subprocess.run(["bash", str(repo / "scripts" / "run_alerts.sh"), "--config", str(ws / "config" / "settings.yaml"),
                        "-p", "p", "--mode", "report"], env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0
    log = (repo / "logs" / "alerts-cron.log").read_text()
    assert "litsuite alerts --config" in log and "Summary for 'p'" in log


def test_scheduler_files_are_well_formed():
    import plistlib

    plist = plistlib.loads((ROOT / "scripts" / "com.litsuite.alerts.plist").read_bytes())
    assert plist["Label"] == "com.litsuite.alerts" and plist["ProgramArguments"][1].endswith("scripts/run_alerts.sh")
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 40}
    for sh in ("install.sh", "run_alerts.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / sh)]).returncode == 0
    ps = (ROOT / "scripts" / "run_alerts.ps1").read_text()
    assert "litsuite alerts @argsList" in ps and "--all-projects" in ps


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("LITSUITE_SKIP_INSTALL_TEST") == "1", reason="install test disabled")
def test_install_script_in_clean_copy(tmp_path):
    """scripts/install.sh on a clean copy of the repository: venv + pip install + working command."""
    repo = tmp_path / "repo"
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.egg-info", "projects", "tests", ".pytest_cache")
    shutil.copytree(ROOT, repo, ignore=ignore)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRTUAL_ENV")}
    p = subprocess.run(["bash", "scripts/install.sh"], cwd=repo, env=env, capture_output=True, text=True, timeout=1500)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    assert (repo / "config" / "settings.yaml").exists()
    v = subprocess.run([str(repo / ".venv" / "bin" / "litsuite"), "--version"], capture_output=True, text=True)
    assert v.returncode == 0 and "litsuite 2." in v.stdout
    d = subprocess.run([str(repo / ".venv" / "bin" / "litsuite"), "doctor"], cwd=repo, capture_output=True, text=True)
    for component in ("keyring", "playwright", "googleapiclient", "msal", "dropbox", "pandas"):
        assert f"[x] {component}" in d.stdout, d.stdout
