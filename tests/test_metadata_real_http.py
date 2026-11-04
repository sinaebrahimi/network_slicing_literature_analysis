"""metadata.py / http.py / catalog enrichment / Unpaywall through the real `requests`
stack. Only the network is replaced (by `responses`), with payloads shaped like the
real CrossRef, Semantic Scholar, OpenAlex, arXiv, Unpaywall and IEEE responses."""

import json
import re

import pytest
import responses

import litsuite.http as lhttp
from litsuite import metadata as md
from litsuite.catalog import fill_abstracts, read_catalog, scan_libraries, update_catalog, write_catalog
from litsuite.fetch.downloader import unpaywall_pdfs
from litsuite.storage import LocalStorage

ABS = ("Network slicing enables operators to run multiple logical networks over shared infrastructure. "
       "We propose a multi-agent deep reinforcement learning scheme for end-to-end resource allocation.")

CR_ITEM = {
    "DOI": "10.1109/TNSM.2024.3361234", "type": "journal-article", "publisher": "IEEE",
    "title": ["Multi-Agent Deep Reinforcement Learning for End-to-End Network Slicing"],
    "author": [{"given": "Wei", "family": "Liu", "sequence": "first"}, {"given": "Ana", "family": "Silva"}],
    "container-title": ["IEEE Transactions on Network and Service Management"],
    "published-print": {"date-parts": [[2024, 6]]}, "issued": {"date-parts": [[2024, 3]]},
    "abstract": f"<jats:p>{ABS}</jats:p>", "URL": "https://doi.org/10.1109/TNSM.2024.3361234",
    "link": [{"URL": "https://ieeexplore.ieee.org/ielx7/x.pdf", "content-type": "application/pdf",
              "intended-application": "text-mining"}],
}
S2_PAPER = {"paperId": "abc", "title": "Multi-Agent Deep Reinforcement Learning for End-to-End Network Slicing",
            "externalIds": {"DOI": "10.1109/TNSM.2024.3361234"}, "abstract": None, "year": 2024,
            "authors": [{"authorId": "1", "name": "Wei Liu"}], "venue": "IEEE Transactions on Network and Service Management",
            "publicationVenue": {"name": "IEEE Transactions on Network and Service Management"},
            "openAccessPdf": None, "publicationTypes": ["JournalArticle"]}
OPENALEX = {"title": "Multi-Agent Deep Reinforcement Learning for End-to-End Network Slicing",
            "doi": "https://doi.org/10.1109/tnsm.2024.3361234", "publication_year": 2024,
            "authorships": [{"author": {"display_name": "Wei Liu"}}],
            "primary_location": {"source": {"display_name": "IEEE Transactions on Network and Service Management"}},
            "abstract_inverted_index": {w: [i] for i, w in enumerate(ABS.split())} | {},
            "best_oa_location": {"pdf_url": "https://oa.example.org/liu.pdf"}, "locations": []}
ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2503.04184v1</id>
    <published>2025-03-06T08:00:00Z</published>
    <title>Large-Scale AI in Telecom: Charting the Roadmap
      for Innovation</title>
    <summary>  This white paper discusses the role of large-scale AI in the telecommunications industry,
      with a focus on generative AI for 6G network functions and user experiences.  </summary>
    <author><name>Arsalan Shahid</name></author>
    <author><name>Adrian Kliks</name></author>
    <arxiv:journal_ref>IEEE Network 2025</arxiv:journal_ref>
  </entry>
</feed>"""
IEEE_PAGE = ('<html><head><title>IEEE Xplore</title></head><body><script>xplGlobal.document.metadata='
             '{"title":"x","abstract":"' + ABS.replace('"', '\\"') + ' Results show 20% gains.","doi":"10.1109/X"};'
             '</script></body></html>')


@pytest.fixture
def http():
    h = lhttp.Http("lab@example.org", delays={k: 0 for k in lhttp.HOST_DELAYS})
    return h


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(lhttp.time, "sleep", lambda s: None)


# ---------------------------------------------------------------- http.py
@responses.activate
def test_http_etiquette_and_retries(http, no_sleep):
    responses.add(responses.GET, re.compile(r"https://api\.crossref\.org/works.*"), status=429,
                  headers={"Retry-After": "1"})
    responses.add(responses.GET, re.compile(r"https://api\.crossref\.org/works.*"), status=503)
    responses.add(responses.GET, re.compile(r"https://api\.crossref\.org/works.*"), json={"message": {"items": []}})
    r = http.get("https://api.crossref.org/works", params={"query.bibliographic": "x"})
    assert r.status_code == 200 and len(responses.calls) == 3
    req = responses.calls[-1].request
    assert "mailto=lab%40example.org" in req.url                 # CrossRef "polite pool"
    assert "litsuite/" in req.headers["User-Agent"] and "mailto:lab@example.org" in req.headers["User-Agent"]
    responses.add(responses.GET, "https://www.publisher.example/page", body="<html/>")
    http.get("https://www.publisher.example/page")
    assert "Mozilla" in responses.calls[-1].request.headers["User-Agent"]   # browser UA for web pages
    # Network failure -> None, never an exception
    assert http.get("https://unreachable.example/") is None
    assert http.get_json("https://www.publisher.example/page") is None  # not JSON


def test_http_throttles_per_host(monkeypatch):
    slept = []
    clock = iter([0.0, 0.1, 0.1])
    monkeypatch.setattr(lhttp.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(lhttp.time, "sleep", slept.append)
    h = lhttp.Http(delays={"api.example.org": 1.0})
    h._throttle("api.example.org")
    h._throttle("api.example.org")
    assert len(slept) == 1 and abs(slept[0] - 0.9) < 1e-6   # first request never waits


# ---------------------------------------------------------------- metadata.py
@responses.activate
def test_crossref_doi_search_pii(http):
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234",
                  json={"status": "ok", "message": CR_ITEM})
    m = md.crossref_by_doi("https://doi.org/10.1109/TNSM.2024.3361234", http)
    assert (m.title, m.authors, m.year, m.pub_type) == (CR_ITEM["title"][0], ["Liu", "Silva"], "2024", "journal")
    assert m.abstract == ABS and m.pdf_urls == []                 # text-mining links are not used
    other = dict(CR_ITEM, title=["Something else entirely about optics"])
    responses.add(responses.GET, "https://api.crossref.org/works",
                  json={"message": {"items": [other, CR_ITEM]}})
    hit = md.crossref_search("multi agent deep reinforcement learning for end to end network slicing", "Liu", "2024", http)
    assert hit.doi == CR_ITEM["DOI"]
    assert "query.author=Liu" in responses.calls[-1].request.url
    assert md.crossref_search("A totally unrelated title about bananas", http=http) is None
    assert md.crossref_search(CR_ITEM["title"][0], year="2015", http=http).doi    # exact title beats year gap
    responses.add(responses.GET, "https://api.crossref.org/works",
                  json={"message": {"items": [dict(CR_ITEM, DOI="10.1016/j.comnet.2026.1")]}})
    assert md.crossref_by_pii("S1389128626004321", http).doi == "10.1016/j.comnet.2026.1"
    assert "alternative-id%3AS1389128626004321" in responses.calls[-1].request.url


@responses.activate
def test_semantic_scholar_openalex_arxiv(http):
    responses.add(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/search",
                  json={"total": 2, "data": [dict(S2_PAPER, title="Unrelated"), S2_PAPER]})
    s = md.s2_search(S2_PAPER["title"], "2024", http)
    assert s.doi == "10.1109/TNSM.2024.3361234" and s.authors == ["Liu"] and s.pub_type == "journal"
    assert "year=2024" in responses.calls[-1].request.url and "openAccessPdf" in responses.calls[-1].request.url
    responses.add(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/ARXIV:2503.04184",
                  json=dict(S2_PAPER, externalIds={"ArXiv": "2503.04184"}, openAccessPdf={"url": "https://arxiv.org/pdf/2503.04184"}))
    assert md.s2_by_id("ARXIV:2503.04184", http).pdf_urls == ["https://arxiv.org/pdf/2503.04184"]
    responses.add(responses.GET, "https://api.openalex.org/works/doi:10.1109/TNSM.2024.3361234", json=OPENALEX)
    o = md.openalex_by_doi("10.1109/TNSM.2024.3361234", http)
    assert o.abstract == ABS and o.pdf_urls == ["https://oa.example.org/liu.pdf"] and o.doi == "10.1109/tnsm.2024.3361234"
    assert "mailto=lab%40example.org" in responses.calls[-1].request.url
    responses.add(responses.GET, "https://export.arxiv.org/api/query", body=ARXIV_ATOM)
    a = md.arxiv_by_id("2503.04184", http)
    assert a.title == "Large-Scale AI in Telecom: Charting the Roadmap for Innovation"
    assert a.authors == ["Shahid", "Kliks"] and a.year == "2025" and a.published_venue == "IEEE Network 2025"
    assert a.pdf_urls == ["https://arxiv.org/pdf/2503.04184"] and a.pub_type == "preprint"
    responses.add(responses.GET, "https://export.arxiv.org/api/query", body="<feed xmlns='http://www.w3.org/2005/Atom'/>")
    assert md.arxiv_by_id("0000.00000", http) is None


@responses.activate
def test_doi_landing_page_abstracts(http):
    responses.add(responses.GET, "https://doi.org/10.1109/X", status=302,
                  headers={"Location": "https://ieeexplore.ieee.org/document/1234567"})
    responses.add(responses.GET, "https://ieeexplore.ieee.org/document/1234567", body=IEEE_PAGE)
    assert md.doi_page_abstract("10.1109/X", http).startswith("Network slicing enables operators")
    meta_page = f'<html><head><meta name="citation_abstract" content="Abstract: {ABS}"></head></html>'
    responses.add(responses.GET, "https://doi.org/10.1016/Y", status=302,
                  headers={"Location": "https://www.sciencedirect.com/science/article/pii/S1"})
    responses.add(responses.GET, "https://www.sciencedirect.com/science/article/pii/S1", body=meta_page)
    assert md.doi_page_abstract("10.1016/Y", http) == ABS
    cookie_wall = '<meta name="description" content="This website uses cookies to improve your experience' + "." * 90 + '">'
    responses.add(responses.GET, "https://doi.org/10.1/Z", body=cookie_wall)
    assert md.doi_page_abstract("10.1/Z", http) == ""             # cookie banners are not abstracts


@responses.activate
def test_fetch_abstract_chain_order(http):
    """CrossRef has no abstract -> S2 has none -> OpenAlex provides it."""
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234",
                  json={"message": dict(CR_ITEM, abstract="")})
    responses.add(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1109/TNSM.2024.3361234",
                  json=S2_PAPER)
    responses.add(responses.GET, "https://api.openalex.org/works/doi:10.1109/TNSM.2024.3361234", json=OPENALEX)
    assert md.fetch_abstract("10.1109/TNSM.2024.3361234", "t", http) == (ABS, "openalex")
    # A non-standard DOI (HAL id) goes straight to the S2 title search.
    responses.add(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/search",
                  json={"data": [dict(S2_PAPER, abstract=ABS)]})
    assert md.fetch_abstract("hal-01234", S2_PAPER["title"], http) == (ABS, "s2_title")


@responses.activate
def test_lookup_by_title_merges_s2_and_crossref(http):
    responses.add(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/search", json={"data": [S2_PAPER]})
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234", json={"message": CR_ITEM})
    m = md.lookup_by_title(S2_PAPER["title"], "2024", "", http)
    assert m.venue_full == "IEEE Transactions on Network and Service Management" and m.abstract == ABS
    assert m.sources[:2] == ["crossref", "semanticscholar"]


# ---------------------------------------------------------------- catalog with real lookups
@responses.activate
def test_catalog_update_with_real_lookup_chain(tmp_path, http):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "TNSM 24 Liu - Multi-Agent_DRL_for_E2E_NS.pdf").write_bytes(b"%PDF")
    (lib / "PREP 25 Shahid - Large-Scale_AI_in_Telecom.pdf").write_bytes(b"%PDF")
    (lib / "XYZ 21 - A_paper_nobody_indexed.pdf").write_bytes(b"%PDF")

    def s2_search(request):
        q = request.params.get("query", "")
        if "Multi-Agent DRL for E2E NS" in q:
            return 200, {}, json.dumps({"data": [dict(S2_PAPER, title="Multi-Agent DRL for E2E NS")]})
        if "Large-Scale AI in Telecom" in q:
            paper = dict(S2_PAPER, title="Large-Scale AI in Telecom", venue="IEEE Network",
                         externalIds={"DOI": "10.1109/MNET.2025.1"}, authors=[{"name": "Arsalan Shahid"}])
            return 200, {}, json.dumps({"data": [paper]})
        return 200, {}, json.dumps({"data": []})

    responses.add_callback(responses.GET, "https://api.semanticscholar.org/graph/v1/paper/search", callback=s2_search)
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234",
                  json={"message": dict(CR_ITEM, title=["Multi-Agent DRL for E2E NS"])})
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/MNET.2025.1", json={"message": dict(
        CR_ITEM, DOI="10.1109/MNET.2025.1", title=["Large-Scale AI in Telecom"],
        **{"container-title": ["IEEE Network"]})})
    responses.add(responses.GET, "https://api.crossref.org/works", json={"message": {"items": []}})
    responses.add(responses.GET, re.compile(r"https://api\.openalex\.org/.*"), status=404)
    cat = tmp_path / "cat.csv"
    res = update_catalog(cat, scan_libraries([LocalStorage(lib)], False), backups_dir=tmp_path / "bk", http=http)
    rows = {r["filename"][:4]: r for r in read_catalog(cat)[0]}
    assert rows["TNSM"]["doi"] == "10.1109/TNSM.2024.3361234" and rows["TNSM"]["abstract"] == ABS
    assert rows["TNSM"]["pub_type"] == "journal" and rows["TNSM"]["reference_key"] == "tnsm24liu"
    # The preprint has been published: venue code learned from the DOI (IEEE style -> MNET).
    assert rows["PREP"]["published_venue"] == "IEEE Network" and rows["PREP"]["venue_acronym"] == "MNET"
    assert [r["filename"] for r in res["published_preprints"]] == ["PREP 25 Shahid - Large-Scale_AI_in_Telecom.pdf"]
    # Not found anywhere: still cataloged from the filename.
    assert rows["XYZ "]["doi"] == "" and rows["XYZ "]["title_official"] == "A paper nobody indexed"


@responses.activate
def test_fill_abstracts_with_real_chain(tmp_path, http, monkeypatch):
    monkeypatch.setattr("litsuite.catalog.default_http", lambda *a: http, raising=False)
    cat = tmp_path / "c.csv"
    write_catalog(cat, [{"title_official": "T", "doi": "10.1109/TNSM.2024.3361234", "abstract": ""}])
    responses.add(responses.GET, "https://api.crossref.org/works/10.1109/TNSM.2024.3361234", json={"message": CR_ITEM})
    res = fill_abstracts(cat, backups_dir=tmp_path / "bk", http=http)
    assert res["filled"] == 1 and res["sources"] == {"crossref": 1}
    assert read_catalog(cat)[0][0]["abstract"] == ABS


@responses.activate
def test_unpaywall(http):
    responses.add(responses.GET, "https://api.unpaywall.org/v2/10.1038/nature12373", json={
        "best_oa_location": {"url_for_pdf": "https://www.nature.com/articles/nature12373.pdf"},
        "oa_locations": [{"url_for_pdf": "https://europepmc.org/x.pdf"}, {"url_for_pdf": None}]})
    assert unpaywall_pdfs("10.1038/nature12373", "lab@example.org", http) == [
        "https://www.nature.com/articles/nature12373.pdf", "https://europepmc.org/x.pdf"]
    assert "email=lab%40example.org" in responses.calls[-1].request.url
    assert unpaywall_pdfs("10.1038/nature12373", "", http) == []      # Unpaywall requires an email
