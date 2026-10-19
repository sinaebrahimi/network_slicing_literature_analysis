from conftest import ROOT

from litsuite.abbreviations import Abbreviator, learn_abbreviations, load_abbreviations, write_suggestions
from litsuite.naming import build_filename, format_title, parse_filename, unique_name
from litsuite.venues import VenueResolver, acronym_from_name, doi_venue_code, learn_venue_codes, load_venue_files

NS_ABBREV = ROOT / "config" / "abbreviations" / "network_slicing.yaml"


# ---------------------------------------------------------------- parsing
def test_parse_classic_patterns():
    p = parse_filename("ACCESS 25 Fryganiotis - Model_Predictive_Control_based_Dynamic_RA_Enabler_for_Resilient_NS.pdf")
    assert (p.venue_raw, p.year, p.author) == ("ACCESS", "2025", "Fryganiotis")
    assert p.title_raw == "Model Predictive Control based Dynamic RA Enabler for Resilient NS"
    p = parse_filename("ICC 23 - 5G_Network_Slicing_for_Mission-critical_use_cases.pdf")
    assert (p.venue_raw, p.year, p.author) == ("ICC", "2023", "")


def test_parse_edge_cases_from_real_library():
    cases = {
        "NetSoft 24 Ameur, Brik - Leveraging LLMs.pdf": ("NetSoft", "2024", "Ameur", "Leveraging LLMs"),
        "JIOT 25 Al-habob - Energy-Efficient Aerial NS.pdf": ("JIOT", "2025", "Al-habob", "Energy-Efficient Aerial NS"),
        "ACCESS 24 Dobreff- A_QoE_Framework.pdf": ("ACCESS", "2024", "Dobreff", "A QoE Framework"),
        "5G-MeMU 21 - Extending Slice Management.pdf": ("5G-MeMU", "2021", "", "Extending Slice Management"),
        "Eur. Wir. 16 - Network_slicing_management.pdf": ("Eur. Wir.", "2016", "", "Network slicing management"),
        "IJCS 21 - Bakri - On using RL for admission.pdf": ("IJCS", "2021", "Bakri", "On using RL for admission"),
        "SIU 25 Saridas (Turkish) - xApp-based RA.pdf": ("SIU", "2025", "Saridas", "xApp-based RA"),
        "CDS 25 Ajayi - PROPHET - Proactive Optimization.pdf": ("CDS", "2025", "Ajayi", "PROPHET - Proactive Optimization"),
        "PREP 2025 Camargo - Towards_Cloud.pdf": ("PREP", "2025", "Camargo", "Towards Cloud"),
        "random name.pdf": ("", "", "", "random name"),
    }
    for fname, expected in cases.items():
        p = parse_filename(fname)
        assert (p.venue_raw, p.year, p.author, p.title_raw) == expected, fname


def test_parse_every_real_filename_has_a_year():
    from litsuite.catalog import read_catalog

    rows, _ = read_catalog(ROOT / "projects" / "network_slicing" / "litreview.csv")
    assert len(rows) > 600
    assert sum(1 for r in rows if not parse_filename(r["filename"]).year) == 0


# ---------------------------------------------------------------- abbreviations
def test_user_example_filename():
    ab = load_abbreviations([NS_ABBREV])
    name = build_filename("MNET", "2025", "Fryganiotis",
                          "Model Predictive Control-Based Dynamic Resource Allocation Enabler for Resilient Network Slicing", ab)
    assert name == "MNET 25 Fryganiotis - Model_Predictive_Control-Based_Dynamic_RA_Enabler_for_Resilient_NS.pdf"
    # The same rule set with Resource Allocation disabled reproduces the README example exactly.
    ab2 = load_abbreviations([NS_ABBREV], {"Resource Allocation": ""})
    assert build_filename("MNET", "2025", "Fryganiotis",
                          "Model Predictive Control-Based Dynamic Resource Allocation Enabler for Resilient Network Slicing",
                          ab2) == ("MNET 25 Fryganiotis - Model_Predictive_Control-Based_Dynamic_Resource_Allocation_"
                                   "Enabler_for_Resilient_NS.pdf")


def test_abbreviation_matching_rules():
    ab = Abbreviator({"Network Slicing": "NS", "Deep Reinforcement Learning": "DRL",
                      "Multi-Agent Deep Reinforcement Learning": "MA-DRL", "End-to-End": "E2E"})
    assert ab.abbreviate("Network_Slicing and network-slicing and NETWORK SLICING") == "NS and NS and NS"
    assert ab.abbreviate("Multi-Agent Deep Reinforcement Learning for End-to-End Slicing") == "MA-DRL for E2E Slicing"
    assert ab.abbreviate("Networks Slicing is not touched") == "Networks Slicing is not touched"
    assert ab.expand("MA-DRL for E2E NS") == "Multi-Agent Deep Reinforcement Learning for End-to-End Network Slicing"
    assert "zzsepzz" in ab.expand("RAN NS", isolate=True)


def test_filename_sanitising():
    name = build_filename("FGCS", "2024", "Gonçalves", 'Slicing: "Overhead" / Performance? <i>A</i> study', None)
    assert name == "FGCS 24 Goncalves - Slicing_Overhead_Performance_A_study.pdf"
    long = build_filename("TNSM", "2025", "Li", "word " * 100, None)
    assert len(long) <= 200 and long.endswith(".pdf")
    assert format_title("A B", None, {"word_separator": " "}) == "A B"
    assert unique_name("a.pdf", {"A.pdf", "a (2).pdf"}) == "a (3).pdf"


def test_learn_abbreviations_from_real_catalog(tmp_path):
    from litsuite.catalog import read_catalog

    rows, _ = read_catalog(ROOT / "projects" / "network_slicing" / "litreview.csv")
    learned = {(p.lower(), s) for p, s, n, _ in learn_abbreviations(rows, 3)}
    assert ("network slicing", "NS") in learned
    assert ("resource allocation", "RA") in learned
    assert ("deep reinforcement learning", "DRL") in learned
    out = write_suggestions(learn_abbreviations(rows, 3), tmp_path / "s.yaml")
    assert "Network Slicing: NS" in out.read_text()
    assert load_abbreviations([out]).abbreviate("Network Slicing") == "NS"


# ---------------------------------------------------------------- venues
def test_doi_venue_codes():
    assert doi_venue_code("10.1109/TNSM.2024.3361234") == "10.1109/tnsm"
    assert doi_venue_code("10.1109/ICC45041.2023.10279084") == "10.1109/icc"
    assert doi_venue_code("10.1016/j.comnet.2023.109876") == "10.1016/comnet"
    assert doi_venue_code("10.1007/s10922-023-09712-1") == "10.1007/s10922"
    assert doi_venue_code("10.3390/electronics12030567") == "10.3390/electronics"
    assert doi_venue_code("10.1002/dac.5432") == "10.1002/dac"
    assert doi_venue_code("10.1145/3472771.3472775") is None
    assert doi_venue_code("10.5281/zenodo.123") is None


def test_venue_resolution_order():
    v = VenueResolver(load_venue_files([ROOT / "config" / "venues" / "common.yaml"]),
                      {"10.1016/future": "FGCS"}, {"10.1016": "j{code}"})
    assert v.resolve("10.48550/arXiv.2401.00001", "arXiv") == "PREP"
    assert v.resolve("10.1016/j.future.2024.01.001", "Future Generation Computer Systems") == "FGCS"
    assert v.resolve("10.1016/j.adhoc.2024.103456", "Some Elsevier Journal") == "jadhoc"
    assert v.resolve("10.1109/TNSM.2024.1", "IEEE Transactions on Network and Service Management") == "TNSM"
    assert v.resolve("10.1145/3570361.3592520",
                     "Proceedings of the 29th Annual International Conference on Mobile Computing and Networking") == "MobiCom"
    assert v.resolve("10.1145/1.2", "Unknown Workshop", "conference", "HotNets '24") == "HotNets"
    assert v.resolve("", "Procedia Computer Science") == "PCS"   # no accidental 'Science' match
    assert acronym_from_name("2023 IEEE International Conference on Communications (ICC)") == "ICC"
    assert acronym_from_name("Journal of Network and Systems Management") == "JNSM"


def test_learn_venue_codes_from_real_catalog():
    from litsuite.catalog import read_catalog

    rows, _ = read_catalog(ROOT / "projects" / "network_slicing" / "litreview.csv")
    learned = learn_venue_codes(rows)
    assert learned["10.1109/tnsm"] == "TNSM"
    assert learned["10.1016/future"] == "FGCS"
    assert learned["10.1002/dac"] == "DAC"
    assert not any("arxiv" in k for k in learned)
