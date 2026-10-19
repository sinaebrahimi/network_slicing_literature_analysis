"""Publisher knowledge: where to sign in and where the PDF of an article lives.

Every value can be overridden in settings.yaml, e.g.::

    access:
      publishers:
        ieee:
          pdf_templates: ["https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}"]
        mypublisher:                 # add a new one
          name: My Publisher
          doi_prefixes: ["10.9999"]
          domains: ["journals.mypublisher.org"]
          login_url: "https://journals.mypublisher.org/login"
          pdf_templates: ["https://journals.mypublisher.org/doi/pdf/{doi}"]

Template placeholders: {doi} {doi_quoted} {arnumber} {pii} {arxiv_id} {nature_id}
WAYFless placeholders: {entity_id} and {target} (both URL-encoded automatically)
"""

from __future__ import annotations

from urllib.parse import quote

from litsuite.config import deep_merge

PUBLISHERS = {
    "ieee": {
        "name": "IEEE Xplore",
        "doi_prefixes": ["10.1109", "10.23919"],
        "domains": ["ieeexplore.ieee.org"],
        "home": "https://ieeexplore.ieee.org/Xplore/home.jsp",
        "login_url": "https://ieeexplore.ieee.org/Xplore/home.jsp",
        "login_hint": "Click 'Institutional Sign In' (top right), search for your university and sign in.",
        "wayfless": "https://ieeexplore.ieee.org/servlet/wayf.jsp?entityId={entity_id}&url={target}",
        "pdf_templates": ["https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}&ref="],
        "access_markers": ["Access provided by"],
    },
    "elsevier": {
        "name": "ScienceDirect (Elsevier)",
        "doi_prefixes": ["10.1016"],
        "domains": ["sciencedirect.com", "elsevier.com"],
        "home": "https://www.sciencedirect.com/",
        "login_url": "https://www.sciencedirect.com/user/institution/login",
        "login_hint": "Choose 'Sign in via your institution', find your university and sign in.",
        "wayfless": "https://auth.elsevier.com/ShibAuth/institutionLogin?entityID={entity_id}&appReturnURL={target}",
        "pdf_templates": ["https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true"],
        "access_markers": ["Brought to you by", "Access through your institution"],
    },
    "springer": {
        "name": "SpringerLink",
        "doi_prefixes": ["10.1007", "10.1186"],
        "domains": ["link.springer.com", "springeropen.com", "biomedcentral.com"],
        "home": "https://link.springer.com/",
        "login_url": "https://link.springer.com/",
        "login_hint": "Click 'Log in' > 'Access via your institution', find your university and sign in.",
        "wayfless": "https://fsso.springer.com/saml/login?idp={entity_id}&targetUrl={target}",
        "pdf_templates": ["https://link.springer.com/content/pdf/{doi}.pdf"],
    },
    "nature": {
        "name": "Nature Portfolio",
        "doi_prefixes": ["10.1038"],
        "domains": ["nature.com"],
        "home": "https://www.nature.com/",
        "login_url": "https://www.nature.com/",
        "login_hint": "Click 'Log in' > 'Access through your institution', find your university and sign in.",
        "wayfless": "",
        "pdf_templates": ["https://www.nature.com/articles/{nature_id}.pdf"],
    },
    "acm": {
        "name": "ACM Digital Library",
        "doi_prefixes": ["10.1145"],
        "domains": ["dl.acm.org"],
        "home": "https://dl.acm.org/",
        "login_url": "https://dl.acm.org/action/ssostart",
        "login_hint": "Search for your university under 'Institutional Login' and sign in.",
        "wayfless": "https://dl.acm.org/action/ssostart?idp={entity_id}&redirectUri={target}",
        "pdf_templates": ["https://dl.acm.org/doi/pdf/{doi}"],
    },
    "wiley": {
        "name": "Wiley Online Library",
        "doi_prefixes": ["10.1002", "10.1111", "10.1049"],
        "domains": ["onlinelibrary.wiley.com", "ietresearch.onlinelibrary.wiley.com"],
        "home": "https://onlinelibrary.wiley.com/",
        "login_url": "https://onlinelibrary.wiley.com/action/ssostart",
        "login_hint": "Search for your university under 'Institutional Login' and sign in.",
        "wayfless": "https://onlinelibrary.wiley.com/action/ssostart?idp={entity_id}&redirectUri={target}",
        "pdf_templates": ["https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"],
    },
    "tandf": {
        "name": "Taylor & Francis Online",
        "doi_prefixes": ["10.1080"],
        "domains": ["tandfonline.com"],
        "home": "https://www.tandfonline.com/",
        "login_url": "https://www.tandfonline.com/action/ssostart",
        "login_hint": "Search for your university under 'Institutional Login' and sign in.",
        "wayfless": "https://www.tandfonline.com/action/ssostart?idp={entity_id}&redirectUri={target}",
        "pdf_templates": ["https://www.tandfonline.com/doi/pdf/{doi}?download=true"],
    },
    "sage": {
        "name": "SAGE Journals",
        "doi_prefixes": ["10.1177"],
        "domains": ["journals.sagepub.com"],
        "home": "https://journals.sagepub.com/",
        "login_url": "https://journals.sagepub.com/action/ssostart",
        "wayfless": "https://journals.sagepub.com/action/ssostart?idp={entity_id}&redirectUri={target}",
        "pdf_templates": ["https://journals.sagepub.com/doi/pdf/{doi}?download=true"],
    },
    "mdpi": {
        "name": "MDPI (open access)",
        "doi_prefixes": ["10.3390"],
        "domains": ["mdpi.com"],
        "home": "https://www.mdpi.com/",
        "login_url": "",
        "wayfless": "",
        "pdf_templates": [],
    },
    "arxiv": {
        "name": "arXiv (open access)",
        "doi_prefixes": ["10.48550"],
        "domains": ["arxiv.org"],
        "home": "https://arxiv.org/",
        "login_url": "",
        "wayfless": "",
        "pdf_templates": ["https://arxiv.org/pdf/{arxiv_id}"],
    },
}

LOGIN_PUBLISHERS = ["ieee", "elsevier", "springer", "nature", "acm", "wiley", "tandf", "sage"]


def publishers(overrides: dict | None = None) -> dict:
    return deep_merge(PUBLISHERS, overrides or {})


def publisher_for(doi: str = "", url: str = "", table: dict | None = None):
    table = table or PUBLISHERS
    doi = (doi or "").lower()
    url = (url or "").lower()
    for key, p in table.items():
        if doi and any(doi.startswith(pref.lower() + "/") for pref in p.get("doi_prefixes", [])):
            return key
    for key, p in table.items():
        if url and any(d in url for d in p.get("domains", [])):
            return key
    return None


def login_url(pub: dict, access: dict) -> str:
    """Institutional sign-in URL: WAYFless if your IdP entityID is configured."""
    entity = (access or {}).get("idp_entity_id", "")
    if entity and pub.get("wayfless"):
        return pub["wayfless"].format(entity_id=quote(entity, safe=""), target=quote(pub.get("home", ""), safe=""))
    return pub.get("login_url") or pub.get("home", "")


def pdf_urls(pub: dict, ids: dict) -> list:
    """Fill the publisher's PDF templates with the identifiers we know."""
    values = {k: v for k, v in ids.items() if v}
    if values.get("doi"):
        values["doi_quoted"] = quote(values["doi"], safe="")
        if values["doi"].lower().startswith("10.1038/") and "nature_id" not in values:
            values["nature_id"] = values["doi"].split("/", 1)[1]
    out = []
    for tpl in pub.get("pdf_templates", []):
        try:
            out.append(tpl.format(**values))
        except (KeyError, IndexError):
            continue
    return out


def via_proxy(url: str, access: dict) -> str:
    """Route a URL through EZproxy or the OpenAthens redirector if configured."""
    access = access or {}
    if access.get("ezproxy_prefix"):
        return access["ezproxy_prefix"] + quote(url, safe="")
    if access.get("openathens_domain"):
        return f"https://go.openathens.net/redirector/{access['openathens_domain']}?url={quote(url, safe='')}"
    return url
