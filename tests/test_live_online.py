"""Live checks against the real services. Opt-in:  pytest -m online
They skip themselves when the services cannot be reached (e.g. offline or behind a firewall)."""

import pytest

from conftest import ROOT

pytestmark = pytest.mark.online

KIM_DOI = "10.1109/ACCESS.2021.3072435"
KIM_TITLE = "Multi-Agent Reinforcement Learning-Based Resource Management for End-to-End Network Slicing"


@pytest.fixture(scope="module")
def http():
    from litsuite.http import Http

    h = Http("")
    r = h.get("https://api.crossref.org/works?rows=0", timeout=15, retries=0)
    if r is None or r.status_code != 200:
        pytest.skip("scholarly APIs are not reachable from this machine")
    return h


def test_crossref_semantic_scholar_openalex(http):
    from litsuite import metadata as md

    cr = md.crossref_by_doi(KIM_DOI, http)
    assert cr.title == KIM_TITLE and cr.first_author == "Kim" and cr.year == "2021" and cr.pub_type == "journal"
    hit = md.crossref_search(KIM_TITLE, "Kim", "2021", http)
    assert hit.doi.lower() == KIM_DOI.lower()
    s2 = md.s2_search(KIM_TITLE, "2021", http)
    assert s2 is None or s2.doi.lower() == KIM_DOI.lower()        # S2 rate-limits anonymous users at times
    oa = md.openalex_by_doi(KIM_DOI, http)
    assert oa.title.lower() == KIM_TITLE.lower()
    abstract, source = md.fetch_abstract(KIM_DOI, KIM_TITLE, http)
    assert len(abstract) > 300 and source != "none"


def test_arxiv_and_open_access_download(http, tmp_path):
    from litsuite import metadata as md
    from litsuite.fetch.downloader import PaperDownloader

    a = md.arxiv_by_id("1706.03762", http)
    assert a.title == "Attention Is All You Need" and a.first_author == "Vaswani"
    dl = PaperDownloader({"method": "none"}, http=http)
    res = dl.download(a, tmp_path / "attention.pdf")
    assert res.ok and (tmp_path / "attention.pdf").stat().st_size > 100_000


def test_unpaywall_and_venue_naming(http):
    from litsuite.abbreviations import load_abbreviations
    from litsuite.catalog import read_catalog
    from litsuite.fetch.downloader import unpaywall_pdfs
    from litsuite.metadata import crossref_by_doi
    from litsuite.naming import build_filename
    from litsuite.venues import VenueResolver, learn_venue_codes

    assert unpaywall_pdfs("10.1038/nature12373", "litsuite-tests@example.org", http)
    rows, _ = read_catalog(ROOT / "projects" / "network_slicing" / "litreview.csv")
    meta = crossref_by_doi(KIM_DOI, http)
    venue = VenueResolver(learned_codes=learn_venue_codes(rows)).resolve(meta.doi, meta.venue_full, meta.pub_type)
    ab = load_abbreviations([ROOT / "config" / "abbreviations" / "network_slicing.yaml"])
    assert build_filename(venue, meta.year, meta.first_author, meta.title, ab) == \
        "ACCESS 21 Kim - MARL-Based_Resource_Management_for_E2E_NS.pdf"


def test_catalog_enrichment_of_real_filenames(http, tmp_path):
    from litsuite.catalog import read_catalog, scan_libraries, update_catalog
    from litsuite.storage import LocalStorage

    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ["ACCESS 21 Kim - Multi-Agent_Reinforcement_Learning-Based_Resource_Management_for_End-to-End_Network_Slicing.pdf",
                 "TNSM 24 Liu - RL-Based_Network_Slicing_Scheme_for_Optimized_UE-QoS_in_Future_Networks.pdf"]:
        (lib / name).write_bytes(b"%PDF")
    update_catalog(tmp_path / "c.csv", scan_libraries([LocalStorage(lib)], False), http=http)
    rows = {r["first_author"]: r for r in read_catalog(tmp_path / "c.csv")[0]}
    assert rows["Kim"]["doi"].lower() == KIM_DOI.lower() and rows["Kim"]["abstract"]
    assert rows["Liu"]["doi"].lower() == "10.1109/tnsm.2024.3368294"


def test_ieee_landing_page_identification(http):
    from litsuite.metadata import identify_landing_page

    page = identify_landing_page("https://ieeexplore.ieee.org/document/9399427", http)
    if not page:
        pytest.skip("IEEE Xplore refused the automated request from this network")
    assert page["doi"].lower().startswith("10.1109/")
