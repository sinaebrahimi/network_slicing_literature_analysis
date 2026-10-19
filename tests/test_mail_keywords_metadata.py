import base64
import datetime as dt

from conftest import FIXTURES, ROOT

from litsuite.abbreviations import load_abbreviations
from litsuite.config import DEFAULT_SCORING
from litsuite.keywords import RelevanceScorer, suggest_keywords
from litsuite.mail.base import parse_mime, sender_matches
from litsuite.mail.folder import FolderMailbox
from litsuite.mail.gmail import GmailMailbox
from litsuite.mail.imap import ImapMailbox
from litsuite.mail.links import identify, unwrap_url
from litsuite.mail.outlook import OutlookMailbox
from litsuite.mail.parsers import all_sources, match_source, parse_message
from litsuite.metadata import _from_crossref, _from_s2, _inverted_to_text, normalize_doi

EMAILS = FIXTURES / "emails"
SINCE = dt.date(2026, 1, 1)


def load(name):
    return parse_mime((EMAILS / name).read_bytes(), account="test")


# ---------------------------------------------------------------- links
def test_unwrap_tracking_links():
    assert unwrap_url("https://scholar.google.com/scholar_url?url=https://arxiv.org/pdf/2503.04184&amp;hl=en&amp;sa=X") \
        == "https://arxiv.org/pdf/2503.04184"
    assert unwrap_url("https://eur02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fieeexplore.ieee.org"
                      "%2Fdocument%2F11099887%2F&data=05") == "https://ieeexplore.ieee.org/document/11099887/"
    assert unwrap_url("https://click.notification.elsevier.com/CL0/https:%2F%2Fwww.sciencedirect.com%2Fscience"
                      "%2Farticle%2Fpii%2FS1389128626004321%3Fdgcid%3Dx/1/abc").startswith(
        "https://www.sciencedirect.com/science/article/pii/S1389128626004321")
    assert unwrap_url("https://urldefense.proofpoint.com/v2/url?u=https-3A__dl.acm.org_doi_10.1145_1.2&d=x") \
        == "https://dl.acm.org/doi/10.1145/1.2"
    assert unwrap_url("https://dl.acm.org/doi/10.1145/3712345.3712346") == "https://dl.acm.org/doi/10.1145/3712345.3712346"


def test_identify_article_urls():
    assert identify("https://ieeexplore.ieee.org/abstract/document/11012345/")["ieee"] == "11012345"
    assert identify("https://www.sciencedirect.com/science/article/pii/S1389128626004321")["pii"] == "S1389128626004321"
    assert identify("https://link.springer.com/article/10.1007/s10922-023-09712-1")["doi"] == "10.1007/s10922-023-09712-1"
    assert identify("https://www.nature.com/articles/s41598-025-01234-5")["doi"] == "10.1038/s41598-025-01234-5"
    assert identify("https://arxiv.org/pdf/2503.04184v2")["arxiv"] == "2503.04184"
    assert identify("https://onlinelibrary.wiley.com/doi/full/10.1002/dac.5432")["doi"] == "10.1002/dac.5432"
    assert identify("https://dl.acm.org/doi/pdf/10.1145/1.2")["is_pdf"]


# ---------------------------------------------------------------- parsers
def test_sender_patterns():
    assert sender_matches("sciencedirect@notification.elsevier.com", ["@elsevier.com"])
    assert sender_matches("x@acm.org", ["*@acm.org"])
    assert not sender_matches("x@notacm.org", ["@acm.org"])
    assert sender_matches("scholaralerts-noreply@google.com", ["scholaralerts-noreply@google.com"])


def test_google_scholar_alert():
    src = all_sources()
    msg = load("scholar_alert.eml")
    assert match_source(msg, src) == "google_scholar"
    cands = parse_message(msg, "google_scholar", src)
    assert len(cands) == 4                       # share/save/list/cancel links are ignored
    first = cands[0]
    assert first.title.startswith("Hierarchical Multi-Agent Deep Reinforcement Learning")
    assert first.pdf_url.endswith("/oa/hmadrl.pdf") and first.authors == ["Doe", "Smith", "Lee"]
    assert first.year == "2025" and first.venue_line.startswith("arXiv preprint")
    assert "end-to-end network slicing" in first.snippet
    assert cands[1].ieee_arnumber == "11012345" and cands[1].venue_line.startswith("IEEE Transactions")
    assert cands[2].doi == "10.1038/s41598-025-01234-5"
    assert cands[3].authors == ["Raftopoulos"]   # "et al." dropped
    assert all(c.source == "google_scholar" and c.message_id for c in cands)


def test_publisher_alerts_and_noise():
    src = all_sources()
    sd = parse_message(load("sciencedirect_alert.eml"), "sciencedirect", src)
    assert [c.pii for c in sd] == ["S1389128626004321", "S1389128626009999"]   # 'View all' / unsubscribe ignored
    ieee = load("ieee_alert.eml")
    assert match_source(ieee, src) == "ieee"
    assert [c.ieee_arnumber for c in parse_message(ieee, "ieee", src)] == ["11099887", "11099888"]
    acm = parse_message(load("acm_toc.eml"), "acm", src)
    assert len(acm) == 1 and acm[0].doi == "10.1145/3712345.3712346"
    arx = parse_message(load("arxiv_digest.eml"), "arxiv", src)
    assert [c.arxiv_id for c in arx] == ["2510.04321", "2510.05555"]
    assert arx[0].title == "Federated Multi-Agent Reinforcement Learning for Network Slicing in Non-Terrestrial Networks"
    assert arx[0].authors == ["Pereira", "Becker"]
    # A colleague's email from an @ieee.org address is not an alert.
    assert match_source(load("personal_ieee.eml"), src) is None


def test_custom_alert_source_from_settings():
    src = all_sources({"my_toc": {"senders": ["toc@journal.example"], "parser": "generic"},
                       "ieee": {"subjects": ["only this"]}})
    assert src["ieee"]["senders"] == ["@ieee.org"] and src["ieee"]["subjects"] == ["only this"]
    assert match_source(load("ieee_alert.eml"), src) is None


# ---------------------------------------------------------------- mailboxes
def test_folder_mailbox(tmp_path):
    (tmp_path / "saved.html").write_text((EMAILS / "scholar_alert.eml").read_text().split("\n\n", 1)[1])
    for f in EMAILS.glob("*.eml"):
        (tmp_path / f.name).write_bytes(f.read_bytes())
    raw = (EMAILS / "scholar_alert.eml").read_bytes()
    (tmp_path / "read_mbox.eml").write_bytes(b"Status: RO\n" + raw.replace(b"scholar-0001", b"scholar-r1"))
    (tmp_path / "read_tb.eml").write_bytes(b"X-Mozilla-Status: 0001\n" + raw.replace(b"scholar-0001", b"scholar-r2"))
    (tmp_path / "unread_tb.eml").write_bytes(b"X-Mozilla-Status: 0000\n" + raw.replace(b"scholar-0001", b"scholar-u3"))
    mb = FolderMailbox({"name": "saved", "path": str(tmp_path)})
    msgs = list(mb.search(["scholaralerts-noreply@google.com"], SINCE))
    # the .eml, the bare .html (sender inferred) and the Thunderbird-unread copy; read copies are skipped
    assert sorted(m.id for m in msgs) == ["<scholar-0001@google.com>", "<scholar-u3@google.com>", "file:saved.html"]
    assert list(mb.search(["@acm.org"], dt.date(2030, 1, 1))) == []


class FakeImap:
    def __init__(self):
        self.raw = {b"7": (EMAILS / "scholar_alert.eml").read_bytes(),
                    b"9": (EMAILS / "personal_ieee.eml").read_bytes()}
        self.calls = []
        self.readonly = None

    def login(self, u, p):
        assert (u, p) == ("me@uni.ac.uk", "app-pass")

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            assert self.readonly is True
            return "OK", [b"7 9"]
        if command == "FETCH":
            assert "PEEK" in args[1]             # fetching never marks mail as read
            return "OK", [(b"7 (BODY[] {n}", self.raw[args[0]])]
        if command == "STORE":
            assert self.readonly is False
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        pass


def test_imap_mailbox_unread_only_and_mark_read():
    fake = FakeImap()
    mb = ImapMailbox({"name": "uni", "host": "imap.uni.ac.uk", "username": "me@uni.ac.uk"},
                     password="app-pass", imap_factory=lambda: fake)
    msgs = list(mb.search(["scholaralerts-noreply@google.com"], SINCE))
    assert [m.sender for m in msgs] == ["scholaralerts-noreply@google.com"]
    search = [args for cmd, args in fake.calls if cmd == "SEARCH"][0]
    assert search[:4] == (None, "UNSEEN", "SINCE", "01-Jan-2026")
    assert msgs[0].handle == ("INBOX", "7")
    assert not any(cmd == "STORE" for cmd, _ in fake.calls)   # nothing changed while reading
    assert mb.mark_read(msgs) == 1
    assert fake.calls[-1] == ("STORE", ("7", "+FLAGS", "(\\Seen)"))


class FakeGmail:
    def __init__(self):
        self.q = None
        raw = base64.urlsafe_b64encode((EMAILS / "sciencedirect_alert.eml").read_bytes()).decode()
        self._raw = raw

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken):
        self.q = q
        return self

    def get(self, userId, id, format):
        assert format == "raw"
        return self

    def batchModify(self, userId, body):
        self.modified = body
        return self

    def execute(self):
        if self.q and not hasattr(self, "_listed"):
            self._listed = True
            return {"messages": [{"id": "abc"}]}
        return {"raw": self._raw}


def test_gmail_mailbox_unread_only():
    svc = FakeGmail()
    mb = GmailMailbox({"name": "g"}, service=svc)
    msgs = list(mb.search(["@notification.elsevier.com"], SINCE))
    assert len(msgs) == 1 and "is:unread" in svc.q and "after:2026/01/01" in svc.q
    assert "notification.elsevier.com" in svc.q
    assert mb.mark_read(msgs) == 1 and svc.modified == {"ids": ["abc"], "removeLabelIds": ["UNREAD"]}


class FakeResp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class FakeGraph:
    def __init__(self):
        self.headers = {}
        self.urls = []
        self.patched = []

    def patch(self, url, json, timeout=60):
        self.patched.append((url, json))
        return FakeResp({})

    def get(self, url, timeout=60):
        self.urls.append(url)
        html = (EMAILS / "ieee_alert.eml").read_text().split("\n\n", 1)[1]
        return FakeResp({"value": [
            {"id": "1", "subject": "IEEE Xplore Saved Search Alert", "from": {"emailAddress": {"address": "onlinealerts@ieee.org"}},
             "receivedDateTime": "2026-10-14T10:00:00Z", "internetMessageId": "<x@ieee.org>",
             "body": {"contentType": "html", "content": html}},
            {"id": "2", "subject": "hi", "from": {"emailAddress": {"address": "friend@example.com"}},
             "receivedDateTime": "2026-10-14T10:00:00Z", "body": {"contentType": "text", "content": "hi"}}]})


def test_outlook_mailbox_unread_only():
    g = FakeGraph()
    mb = OutlookMailbox({"name": "o"}, session=g, token="t")
    msgs = list(mb.search(["@ieee.org"], SINCE))
    assert len(msgs) == 1 and msgs[0].id == "<x@ieee.org>" and "ieeexplore" in msgs[0].html
    assert g.headers["Authorization"] == "Bearer t"
    assert "receivedDateTime ge 2026-01-01T00:00:00Z and isRead eq false" in g.urls[0]
    assert mb.mark_read(msgs) == 1 and g.patched[0][1] == {"isRead": True} and g.patched[0][0].endswith("/messages/1")


# ---------------------------------------------------------------- relevance
def test_relevance_scoring_and_suggestions():
    from litsuite.catalog import read_catalog

    rows, _ = read_catalog(ROOT / "projects" / "network_slicing" / "litreview.csv")
    ab = load_abbreviations([ROOT / "config" / "abbreviations" / "network_slicing.yaml"])
    sugg = suggest_keywords(rows, 30)
    phrases = [p for p, _ in sugg]
    assert phrases[0] == "network slicing" and "resource allocation" in phrases and "while" not in phrases
    sc = RelevanceScorer({"focus": ["network slicing", "resource allocation", "multi-agent reinforcement learning"],
                          "secondary": ["O-RAN", "safe reinforcement learning", "6G"], "exclude": ["protein"]},
                         DEFAULT_SCORING, ab, phrases, [r["title_official"] for r in rows])
    good, reasons = sc.score("Hierarchical MARL for E2E NS in O-RAN")   # abbreviations understood
    assert good > 6 and "focus:multi-agent reinforcement learning" in reasons and "focus:network slicing" in reasons
    ran, reasons = sc.score("Energy-Aware Resource Allocation for RAN Slicing")
    assert "focus:network slicing" not in reasons     # 'RAN' expansion must not fake a phrase match
    bad, _ = sc.score("Deep learning for protein structure prediction")
    assert bad < 0
    meh, _ = sc.score("A survey on blockchain for supply chain finance")
    assert meh < DEFAULT_SCORING["focus_title"]
    strict = RelevanceScorer({"focus": ["network slicing"]}, {**DEFAULT_SCORING, "require_focus": True}, ab)
    assert strict.score("Federated learning for 6G")[0] <= 0


# ---------------------------------------------------------------- metadata parsing
def test_crossref_s2_openalex_parsing():
    item = {"title": ["SafeSlice"], "subtitle": ["Enabling SLA-Compliant O-RAN Slicing"],
            "author": [{"given": "A", "family": "Nagib"}, {"name": "B C Other"}],
            "issued": {"date-parts": [[2025, 5]]}, "container-title": ["2025 IEEE ICMLCN"],
            "DOI": "10.1109/ICMLCN64995.2025.1", "type": "proceedings-article",
            "event": {"name": "ICMLCN 2025", "acronym": "ICMLCN"},
            "abstract": "<jats:p>" + "We propose a safe DRL approach. " * 5 + "</jats:p>",
            "link": [{"URL": "https://x/pdf", "content-type": "application/pdf"}]}
    m = _from_crossref(item)
    assert m.title == "SafeSlice: Enabling SLA-Compliant O-RAN Slicing" and m.authors == ["Nagib", "Other"]
    assert (m.year, m.pub_type, m.event_acronym) == ("2025", "conference", "ICMLCN")
    assert m.abstract.startswith("We propose") and "<" not in m.abstract and m.pdf_urls == ["https://x/pdf"]
    s2 = _from_s2({"title": "T", "externalIds": {"DOI": "10.1/x", "ArXiv": "2401.1"}, "year": 2024,
                   "authors": [{"name": "Jane van Doe"}], "openAccessPdf": {"url": "https://oa/x.pdf"},
                   "publicationTypes": ["JournalArticle"]})
    assert s2.authors == ["Doe"] and s2.pdf_urls == ["https://oa/x.pdf"] and s2.pub_type == "journal"
    assert _inverted_to_text({"slicing": [1], "Network": [0]}) == "Network slicing"
    assert normalize_doi("https://doi.org/10.1109/X.1.") == "10.1109/X.1"
