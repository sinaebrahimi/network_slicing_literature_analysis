import csv

import pytest
import yaml

from conftest import ROOT

import litsuite.catalog as catalog
from litsuite.catalog import find_duplicates, read_catalog, scan_libraries, update_catalog, write_catalog
from litsuite.cli import main
from litsuite.config import ConfigError, Settings, expand_placeholders
from litsuite.metadata import PaperMeta
from litsuite.storage import LocalStorage


def fake_lookup(title, year="", author="", http=None):
    """Stand-in for the online lookup: deterministic metadata derived from the title."""
    if "Mission-critical" in title:
        return PaperMeta(title="5G Network Slicing for Mission-critical use cases", authors=["Roddy"], year="2019",
                         venue_full="IEEE 2nd 5G World Forum", doi="10.1109/5gwf.2019.8911687",
                         abstract="A" * 120, pub_type="conference")
    if "Survey" in title:
        return PaperMeta(title="A Survey on Network Slicing Enabling Technologies", authors=["Wu"], year="2022",
                         doi="10.1109/COMST.2022.1", abstract="", pub_type="journal")
    if "Preprint" in title:
        return PaperMeta(title="My Preprint Title", authors=["Ng"], year="2025", doi="10.1109/TNSM.2025.42",
                         venue_full="IEEE Transactions on Network and Service Management", pub_type="journal",
                         abstract="B" * 100)
    return None


@pytest.fixture
def offline_lookups(monkeypatch):
    monkeypatch.setattr(catalog, "lookup_by_title", fake_lookup)
    monkeypatch.setattr(catalog, "fetch_abstract", lambda doi, title="", http=None: ("Filled abstract " * 8, "test"))


# ---------------------------------------------------------------- config
def test_example_settings_load_and_validate(tmp_path, monkeypatch):
    s = Settings.load(ROOT / "config" / "settings.example.yaml")
    assert s.base_dir == ROOT
    monkeypatch.setattr("litsuite.storage.sync_folders.placeholder_values", lambda: {"home": "/h"})
    prj = s.project("network_slicing")
    assert prj.catalog_path == ROOT / "projects" / "network_slicing" / "litreview.csv"
    assert prj.recursive_setting is True
    assert prj.abbreviator.abbreviate("Network Slicing") == "NS"
    assert prj.alerts["min_score"] == 2.0 and prj.alerts["mode"] == "ask"
    with pytest.raises(ConfigError):   # {onedrive_business} not present on this machine
        _ = prj.library_specs
    warnings = s.validate()
    assert any("onedrive_business" in w for w in warnings)


def test_placeholders(monkeypatch, tmp_path):
    monkeypatch.setattr("litsuite.storage.sync_folders.placeholder_values",
                        lambda: {"onedrive": str(tmp_path / "OneDrive"), "home": str(tmp_path)})
    monkeypatch.setenv("MYVAR", "abc")
    assert expand_placeholders("{onedrive}/PhD/Lit") == f"{tmp_path}/OneDrive/PhD/Lit"
    assert expand_placeholders("%MYVAR%/x") == "abc/x"
    assert expand_placeholders("$MYVAR/x") == "abc/x"
    with pytest.raises(ConfigError):
        expand_placeholders("{gdrive}/x")


def test_multiple_projects_are_separate(copy_config, tmp_path):
    path = copy_config(yaml.safe_dump({"projects": {
        "a": {"library": str(tmp_path / "A"), "keywords": {"focus": ["x"] * 5}},
        "b": {"library": "dropbox:/B", "data_dir": "data/b", "recursive": False},
    }}))
    s = Settings.load(path)
    assert s.project_names == ["a", "b"]
    assert s.project("a").catalog_path != s.project("b").catalog_path
    assert s.project("b").library_specs == ["dropbox:/B"]
    assert s.project("b").recursive_setting is False
    assert any("focus keywords" in w for w in s.validate())
    with pytest.raises(ConfigError):
        s.project()           # ambiguous without a name
    with pytest.raises(ConfigError):
        s.project("nope")


# ---------------------------------------------------------------- catalog
def test_read_cp1252_and_keep_custom_columns(tmp_path):
    p = tmp_path / "old.csv"
    p.write_bytes("title_official,filename,my_notes\nAbstract – with dash,ICC 23 - X.pdf,keep me\n".encode("cp1252"))
    rows, fields = read_catalog(p)
    assert rows[0]["title_official"] == "Abstract – with dash"
    write_catalog(p, rows, fields)
    rows2, fields2 = read_catalog(p)
    assert fields2[-1] == "my_notes" and rows2[0]["my_notes"] == "keep me"
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")   # UTF-8 BOM for Excel


def test_create_then_update(library, tmp_path, offline_lookups):
    st = LocalStorage(library)
    assert dict(st.subfolder_summary()) == {"Surveys": 1, "_old": 1}
    cat = tmp_path / "out" / "lit.csv"
    res = update_catalog(cat, scan_libraries([st], True, exclude=["_old"]), backups_dir=tmp_path / "bk")
    assert len(res["added"]) == 4 and res["total"] == 4
    rows, _ = read_catalog(cat)
    by_name = {r["filename"]: r for r in rows}
    mc = by_name["ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf"]
    assert mc["doi"] == "10.1109/5gwf.2019.8911687" and mc["first_author"] == "Roddy"
    assert mc["reference_key"] == "icc23roddy" and mc["pub_type"] == "conference"
    survey = by_name["COMST 22 Wu - A_Survey_on_NS_Enabling_Technologies.pdf"]
    assert survey["subfolder"] == "Surveys" and survey["abstract"].startswith("Filled abstract")

    # The user edits a row by hand; then files are added, renamed, moved and deleted.
    mc["venue_acronym"] = "5GWF"
    write_catalog(cat, rows, list(rows[0].keys()))
    (library / "ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf").rename(
        library / "Surveys" / "ICC 23 Roddy - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf")
    (library / "ACCESS 25 Fryganiotis - Model_Predictive_Control_based_Dynamic_RA_Enabler_for_Resilient_NS.pdf").unlink()
    (library / "PREP 25 Ng - My_Preprint_Title.pdf").write_bytes(b"%PDF")
    res = update_catalog(cat, scan_libraries([st], True, exclude=["_old"]), backups_dir=tmp_path / "bk")
    assert [r["filename"] for r in res["added"]] == ["PREP 25 Ng - My_Preprint_Title.pdf"]
    assert res["renamed"] == [("ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf",
                               "Surveys/ICC 23 Roddy - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf")]
    assert len(res["missing"]) == 1
    rows, _ = read_catalog(cat)
    moved = [r for r in rows if r["filename"].startswith("ICC 23 Roddy")][0]
    assert moved["venue_acronym"] == "5GWF"          # manual edit kept
    assert moved["doi"] == "10.1109/5gwf.2019.8911687"  # metadata kept, no new lookup
    prep = [r for r in rows if r["filename"].startswith("PREP")][0]
    assert prep["published_venue"].startswith("IEEE Transactions") and prep["venue_acronym"] == "TNSM"
    assert res["published_preprints"]
    assert sum(1 for r in rows if r["status"] == "missing") == 1
    assert list((tmp_path / "bk").glob("lit.*.csv"))

    res = update_catalog(cat, scan_libraries([st], True, exclude=["_old"]), backups_dir=tmp_path / "bk", prune=True)
    assert res["total"] == 4


def test_legacy_v1_rows_are_adopted_not_duplicated(library, tmp_path, offline_lookups):
    cat = tmp_path / "v1.csv"
    with open(cat, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["title_official", "reference_key", "venue_acronym", "year", "doi", "abstract", "first_author", "filename"])
        w.writerow(["Official", "icc23", "ICC", "2023", "10.1/x", "abs", "", "ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf"])
        # a hand-edited v1 row whose filename lost its .pdf extension
        w.writerow(["Kept", "access25", "ACCESS", "2025", "10.1/y", "abs", "Fryganiotis",
                    "ACCESS 25 Fryganiotis - Model_Predictive_Control_based_Dynamic_RA_Enabler_for_Resilient_NS"])
    res = update_catalog(cat, scan_libraries([LocalStorage(library)], False), backups_dir=tmp_path / "bk")
    rows, _ = read_catalog(cat)
    assert len(res["added"]) == 1 and len(rows) == 3 and res["missing"] == []
    assert [r for r in rows if r["title_official"] == "Kept"][0]["filename"].endswith(".pdf")
    legacy = [r for r in rows if r["title_official"] == "Official"][0]
    assert legacy["relative_path"] == "ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf"


def test_find_duplicates():
    rows = [{"title_official": "Latency-Aware Network Slicing in O-RAN", "filename": "a.pdf", "doi": ""},
            {"title_official": "Latency-aware network slicing in O-RAN!", "filename": "b.pdf", "doi": ""},
            {"title_official": "Other", "filename": "c.pdf", "doi": "10.1/z"},
            {"title_official": "Other v2", "filename": "d.pdf", "doi": "10.1/Z"}]
    assert sorted(map(sorted, find_duplicates(rows))) == [["a.pdf", "b.pdf"], ["c.pdf", "d.pdf"]]


def test_fill_abstracts(tmp_path, offline_lookups):
    cat = tmp_path / "c.csv"
    write_catalog(cat, [{"title_official": "T", "doi": "10.1/x", "abstract": ""},
                        {"title_official": "U", "doi": "", "abstract": "has one"}])
    res = catalog.fill_abstracts(cat, backups_dir=tmp_path / "bk")
    assert res["filled"] == 1
    rows, _ = read_catalog(cat)
    assert rows[0]["abstract"].startswith("Filled abstract") and rows[1]["abstract"] == "has one"


# ---------------------------------------------------------------- CLI
def answers(*items):
    it = iter(items)
    return lambda prompt="": next(it)


def test_cli_asks_for_folder_and_subfolders(library, tmp_path, monkeypatch, offline_lookups, capsys):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "cli.csv"
    # folder prompt -> library path (pasted with quotes), subfolder prompt -> select, include Surveys, skip _old
    rc = main(["catalog", "create", "--output", str(out)],
              input_fn=answers(f'"{library}"', "s", "y", "n"))
    assert rc == 0
    rows, _ = read_catalog(out)
    assert {r["subfolder"] for r in rows} == {"", "Surveys"}
    assert "Surveys  (1 PDFs)" in capsys.readouterr().out


def test_cli_flags_skip_questions(library, tmp_path, monkeypatch, offline_lookups):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "flags.csv"
    assert main(["catalog", "update", "--folder", str(library), "--output", str(out), "--no-recursive",
                 "--offline"], input_fn=answers()) == 0
    rows, _ = read_catalog(out)
    assert len(rows) == 3 and all(r["doi"] == "" for r in rows)
    assert main(["catalog", "update", "--folder", str(library), "--output", str(out), "--recursive",
                 "--exclude", "_old", "--offline"], input_fn=answers()) == 0
    assert len(read_catalog(out)[0]) == 4


def test_cli_with_project(copy_config, library, tmp_path, offline_lookups, monkeypatch, capsys):
    path = copy_config(yaml.safe_dump({"projects": {"slicing": {
        "library": str(library), "recursive": True, "exclude_folders": ["_old"],
        "abbreviations": ["config/abbreviations/network_slicing.yaml"],
        "keywords": {"focus": ["network slicing"], "secondary": ["O-RAN"]}}}}))
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(path), "catalog", "update", "-p", "slicing"], input_fn=answers()) == 0
    prj = Settings.load(path).project("slicing")
    assert prj.catalog_path.exists() and len(read_catalog(prj.catalog_path)[0]) == 4
    assert main(["--config", str(path), "abbrev", "test", "-p", "slicing",
                 "Deep Reinforcement Learning for Network Slicing"]) == 0
    assert "DRL_for_NS" in capsys.readouterr().out
    assert main(["--config", str(path), "keywords", "test", "-p", "slicing", "Network slicing in O-RAN"]) == 0
    assert "focus:network slicing" in capsys.readouterr().out
    assert main(["--config", str(path), "keywords", "suggest", "-p", "slicing", "--save"]) == 0
    assert prj.learned_keywords_path.exists()
    assert main(["--config", str(path), "projects"]) == 0
    assert main(["--config", str(path), "rename", "-p", "slicing"]) == 0
    plan = sorted((prj.reports_dir).glob("rename-plan-*.csv"))[0]
    proposed = {p["current"]: p["proposed"] for p in csv.DictReader(open(plan, encoding="utf-8-sig"))}
    # The survey already follows the convention; the old ICC file gains its author and abbreviations.
    assert "Surveys/COMST 22 Wu - A_Survey_on_NS_Enabling_Technologies.pdf" not in proposed
    old = "ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf"
    assert proposed[old] == "ICC 23 Roddy - 5G_NS_for_Mission-critical_use_cases.pdf"
    assert main(["--config", str(path), "rename", "-p", "slicing", "--apply", str(plan)]) == 0
    assert (library / "ICC 23 Roddy - 5G_NS_for_Mission-critical_use_cases.pdf").exists()
    assert not (library / old).exists()
    rows, _ = read_catalog(prj.catalog_path)
    assert any(r["relative_path"] == "ICC 23 Roddy - 5G_NS_for_Mission-critical_use_cases.pdf" for r in rows)


def test_cli_menu_and_errors(capsys):
    assert main(["--config", "/nonexistent/settings.yaml", "projects"]) == 2
    assert "not found" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([], input_fn=answers("99"))


def test_v1_wrapper_scripts_exist():
    for script in ("literature_catalog.py", "fill_abstracts.py"):
        text = (ROOT / script).read_text()
        assert "from litsuite.cli import main" in text


# ---------------------------------------------------------------- dashboard + setup wizard
def test_dashboard_loader(copy_config, tmp_path):
    pytest.importorskip("pandas")
    from litsuite.dashboard import load_dashboard

    ctx = load_dashboard("network_slicing", ROOT / "config" / "settings.example.yaml")
    assert len(ctx.df) == 706 and ctx.figures_dir == ROOT / "projects" / "network_slicing" / "figures"
    assert "network slicing" in ctx.compound_terms and "TNSM" in ctx.journals and "ICC" in ctx.conferences
    # A project without a dashboard file gets its vocabulary from its keywords + abbreviations.
    cat = tmp_path / "other" / "litreview.csv"
    write_catalog(cat, [{"title_official": "Protein design with diffusion models", "year": "2025",
                         "venue_acronym": "Nature", "abstract": "", "status": "present"},
                        {"title_official": "Gone", "year": "2020", "status": "missing"}])
    path = copy_config(yaml.safe_dump({"projects": {"bio": {
        "data_dir": str(cat.parent), "abbreviations": ["config/abbreviations/general.yaml"],
        "keywords": {"focus": ["protein design"], "secondary": ["Machine Learning"]}}}}))
    ctx = load_dashboard("bio", path)
    assert len(ctx.df) == 1
    assert ctx.trend_topics == ["protein design", "machine learning"]
    assert "ml" in ctx.compound_terms["machine learning"]


def test_setup_wizard_creates_settings(tmp_path, library, monkeypatch):
    from litsuite import setup_wizard

    monkeypatch.setattr("litsuite.storage.sync_folders.detect_all", lambda: {})
    target = tmp_path / "config" / "settings.yaml"
    (tmp_path / "config" / "abbreviations").mkdir(parents=True)
    (tmp_path / "config" / "abbreviations" / "general.yaml").write_text(
        (ROOT / "config" / "abbreviations" / "general.yaml").read_text())
    replies = iter(["Protein Design", str(library), "yes", "protein design, structure prediction",
                    "AlphaFold, diffusion", "", "me@uni.ac.uk", "y"])
    assert setup_wizard.run(Settings({}), input_fn=lambda _="": next(replies), printer=lambda *a: None,
                            target=target) == 0
    s = Settings.load(target)
    prj = s.project("protein_design")
    assert prj.library_specs == [str(library)] and prj.recursive_setting is True
    assert prj.keywords["focus"] == ["protein design", "structure prediction"]
    assert s.user_email == "me@uni.ac.uk" and prj.abbreviator.abbreviate("Machine Learning") == "ML"
