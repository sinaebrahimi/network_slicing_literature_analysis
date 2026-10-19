"""Settings: one YAML file describing all your research projects.

Lookup order for the settings file:
  1. ``--config PATH`` on the command line
  2. the ``LITSUITE_CONFIG`` environment variable
  3. ``config/settings.yaml`` or ``settings.yaml`` in the current folder
  4. ``~/.litsuite/settings.yaml``

Relative paths inside the file are resolved against the repository folder
(the parent of ``config/``) or, for a file elsewhere, against its own folder.
Secrets (passwords, tokens) are never stored here: see ``litsuite.secrets``.
"""

from __future__ import annotations

import copy
import os
import re
from functools import cached_property
from pathlib import Path

import yaml

from litsuite.naming import DEFAULT_NAMING

APP_HOME = Path(os.environ.get("LITSUITE_HOME") or (Path.home() / ".litsuite")).expanduser()

DEFAULT_SCORING = {
    "focus_title": 3.0,        # each focus keyword found in the title
    "focus_snippet": 1.0,      # ... found only in the snippet/abstract
    "secondary_title": 1.0,
    "secondary_snippet": 0.5,
    "learned_title": 0.5,      # keywords learned from your library (litsuite keywords suggest)
    "learned_cap": 2.0,        # max total from learned keywords
    "exclude_penalty": 10.0,   # any exclude keyword
    "similarity_weight": 4.0,  # x similarity (0..1) to the papers already in your library
    "require_focus": False,    # True: ignore papers that match no focus keyword
}

DEFAULT_PROJECT = {
    "description": "",
    "library": None,                 # folder path, {onedrive}/..., dropbox:/..., gdrive:/..., onedrive:/..., rclone:remote:path
    "recursive": "ask",              # include PDFs in subfolders: true | false | ask
    "exclude_folders": [],           # subfolder names (or glob patterns) to skip
    "include_folders": [],           # if non-empty, only these subfolders (plus the top level)
    "data_dir": "projects/{project}",
    "catalog": "litreview.csv",      # relative to data_dir
    "abbreviations": [],             # YAML files with "Phrase: SHORT" lines (merged in order)
    "abbreviations_inline": {},
    "venues": ["config/venues/common.yaml"],
    "venue_doi_styles": {},          # e.g. {"10.1016": "j{code}"} -> Elsevier "jcomnet" style
    "naming": {},
    "keywords": {
        "focus": [],                 # 1-3 main topics (strong weight)
        "secondary": [],             # up to ~10 related topics
        "exclude": [],               # topics you never want
        "use_learned": True,         # also use keywords learned from your library
        "use_library_similarity": True,
    },
    "scoring": {},
    "alerts": {
        "accounts": [],              # names from email_accounts
        "sources": ["google_scholar", "sciencedirect", "ieee", "acm", "springer", "wiley", "mdpi", "arxiv"],
        "since_days": 14,
        "mode": "ask",               # ask | auto | report
        "min_score": 2.0,            # show candidates at or above this score
        "auto_download_score": 4.0,  # 'auto' mode downloads candidates at or above this score
        "max_downloads": 25,
        "download_folder": "",       # subfolder of the library for new papers ("" = top level)
        "add_to_catalog": True,
    },
    "dashboard": {},
}

DEFAULT_ACCESS = {
    "open_access_first": True,
    "method": "browser",             # browser | none
    "institution": "",
    "idp_entity_id": "",             # your university's SAML/Shibboleth entityID (optional)
    "ezproxy_prefix": "",            # e.g. https://ezproxy.myuni.ac.uk/login?url=
    "openathens_domain": "",         # e.g. myuni.ac.uk
    "browser": {
        "profile": "default",
        "headless": True,
        "channel": "",               # "" = Playwright Chromium; "chrome" / "msedge" = installed browser
        "executable_path": "",
        "timeout_seconds": 60,
    },
    "publishers": {},                # per-publisher overrides, see litsuite/fetch/publishers.py
}

DEFAULT_SETTINGS = {
    "user": {"email": "", "name": ""},
    "defaults": {},
    "projects": {},
    "email_accounts": {},
    "alert_sources": {},
    "access": DEFAULT_ACCESS,
    "cloud": {"dropbox": {}, "gdrive": {}, "onedrive": {}},
    "network": {"crossref_delay": 0.3, "s2_delay": 0.4, "openalex_delay": 0.2, "page_delay": 0.7},
}

# Keyword list sizes the README recommends (warned about, never enforced).
MAX_FOCUS, MAX_SECONDARY = 3, 10


class ConfigError(Exception):
    pass


def deep_merge(base, override):
    """Recursively merge dicts; lists and scalars in ``override`` replace ``base``."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = deep_merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else copy.deepcopy(v)
    return out


def find_settings_file(explicit=None):
    cands = [explicit, os.environ.get("LITSUITE_CONFIG"),
             Path.cwd() / "config" / "settings.yaml", Path.cwd() / "settings.yaml",
             APP_HOME / "settings.yaml"]
    for c in cands:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser().resolve()
    if explicit:
        raise ConfigError(f"Settings file not found: {explicit}")
    return None


def expand_placeholders(value: str) -> str:
    """Expand ~, $VAR / %VAR% and {onedrive} / {gdrive} / {dropbox} / {icloud} / {box} / {home}."""
    if not isinstance(value, str):
        return value
    if "{" in value:
        from litsuite.storage.sync_folders import placeholder_values

        values = placeholder_values()

        def repl(m):
            key = m.group(1)
            if key in values:
                return values[key]
            if key in ("onedrive", "onedrive_business", "onedrive_personal", "gdrive", "dropbox", "icloud", "box"):
                raise ConfigError(f"{{{key}}} is used in settings but no {key} sync folder was found on this "
                                  f"computer. Install/sign in to the desktop app or use a full path.")
            return m.group(0)

        value = re.sub(r"\{([a-z_]+)\}", repl, value)
    value = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda m: os.environ.get(m.group(1), m.group(0)), value)
    return os.path.expanduser(os.path.expandvars(value))


def is_remote_spec(spec: str) -> bool:
    return bool(re.match(r"^(dropbox|gdrive|onedrive|rclone):", str(spec)))


class Settings:
    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.path = Path(path) if path else None
        self.raw = data or {}
        self.data = deep_merge(DEFAULT_SETTINGS, self.raw)
        if self.path:
            parent = self.path.parent
            self.base_dir = parent.parent if parent.name == "config" else parent
        else:
            self.base_dir = Path.cwd()

    @classmethod
    def load(cls, explicit=None) -> "Settings":
        path = find_settings_file(explicit)
        if not path:
            return cls({}, None)
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{path} is not valid YAML:\n{e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: the top level must be a mapping (key: value)")
        return cls(data, path)

    # -- helpers ---------------------------------------------------------
    def resolve_path(self, p) -> Path:
        p = Path(expand_placeholders(str(p)))
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    @property
    def user_email(self) -> str:
        return (self.data.get("user") or {}).get("email") or os.environ.get("LITSUITE_EMAIL", "")

    @property
    def project_names(self) -> list:
        return list((self.data.get("projects") or {}).keys())

    def project(self, name: str | None = None) -> "Project":
        projects = self.data.get("projects") or {}
        if not name:
            if len(projects) == 1:
                name = next(iter(projects))
            elif not projects:
                raise ConfigError("No projects are defined. Run 'litsuite init' or pass --folder.")
            else:
                raise ConfigError(f"Several projects are defined; choose one with --project "
                                  f"({', '.join(projects)}).")
        if name not in projects:
            raise ConfigError(f"Unknown project '{name}'. Defined: {', '.join(projects) or 'none'}")
        cfg = deep_merge(deep_merge(DEFAULT_PROJECT, self.data.get("defaults") or {}), projects[name] or {})
        return Project(name, cfg, self)

    def adhoc_project(self, folder=None, catalog=None, name: str | None = None) -> "Project":
        """A project defined only by command-line options (no settings entry needed)."""
        cfg = deep_merge(DEFAULT_PROJECT, self.data.get("defaults") or {})
        if folder:
            cfg["library"] = str(folder)
        if catalog:
            catalog = Path(catalog).expanduser().resolve()
            cfg["data_dir"] = str(catalog.parent)
            cfg["catalog"] = catalog.name
        else:
            cfg["data_dir"] = str(Path.cwd())
        nm = name or (Path(str(folder)).name if folder and not is_remote_spec(folder) else "library")
        return Project(nm, cfg, self)

    def email_account(self, name: str) -> dict:
        accounts = self.data.get("email_accounts") or {}
        if name not in accounts:
            raise ConfigError(f"Unknown email account '{name}'. Defined: {', '.join(accounts) or 'none'}")
        return {"name": name, **(accounts[name] or {})}

    @property
    def access(self) -> dict:
        return deep_merge(DEFAULT_ACCESS, self.data.get("access") or {})

    def validate(self) -> list:
        """Human-readable warnings about the settings (never raises)."""
        warnings = []
        if not self.path:
            warnings.append("No settings file found; using built-in defaults.")
        for name in self.project_names:
            try:
                prj = self.project(name)
            except ConfigError as e:
                warnings.append(str(e))
                continue
            kw = prj.cfg.get("keywords") or {}
            if len(kw.get("focus") or []) > MAX_FOCUS:
                warnings.append(f"[{name}] {len(kw['focus'])} focus keywords; 1-{MAX_FOCUS} work best "
                                "(move the rest to 'secondary').")
            if len(kw.get("secondary") or []) > MAX_SECONDARY:
                warnings.append(f"[{name}] {len(kw['secondary'])} secondary keywords; up to {MAX_SECONDARY} "
                                "is recommended.")
            if not prj.cfg.get("library"):
                warnings.append(f"[{name}] no 'library' folder set.")
            else:
                try:
                    prj.library_specs
                except ConfigError as e:
                    warnings.append(f"[{name}] {e}")
            for acc in (prj.cfg.get("alerts") or {}).get("accounts") or []:
                if acc not in (self.data.get("email_accounts") or {}):
                    warnings.append(f"[{name}] alerts uses unknown email account '{acc}'.")
            for f in prj.abbreviation_files + prj.venue_files:
                if not f.exists():
                    warnings.append(f"[{name}] file not found: {f}")
        return warnings


class Project:
    """One research area: its library folder(s), catalog, keywords and naming rules."""

    def __init__(self, name: str, cfg: dict, settings: Settings):
        self.name = name
        self.cfg = cfg
        self.settings = settings

    def __repr__(self):
        return f"Project({self.name!r})"

    # -- locations -------------------------------------------------------
    @property
    def library_specs(self) -> list:
        lib = self.cfg.get("library")
        if not lib:
            return []
        libs = lib if isinstance(lib, list) else [lib]
        return [str(l) if is_remote_spec(l) else str(self.settings.resolve_path(l)) for l in libs]

    @property
    def data_dir(self) -> Path:
        return self.settings.resolve_path(str(self.cfg.get("data_dir") or "projects/{project}")
                                          .replace("{project}", self.name))

    @property
    def catalog_path(self) -> Path:
        cat = Path(expand_placeholders(str(self.cfg.get("catalog") or "litreview.csv")))
        return cat if cat.is_absolute() else self.data_dir / cat

    @property
    def state_dir(self) -> Path:
        return self.data_dir / ".state"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def learned_keywords_path(self) -> Path:
        return self.data_dir / "learned_keywords.yaml"

    # -- rules -----------------------------------------------------------
    @property
    def naming(self) -> dict:
        return {**DEFAULT_NAMING, **(self.cfg.get("naming") or {})}

    @property
    def scoring(self) -> dict:
        return {**DEFAULT_SCORING, **(self.cfg.get("scoring") or {})}

    @property
    def keywords(self) -> dict:
        return self.cfg.get("keywords") or {}

    @property
    def alerts(self) -> dict:
        return deep_merge(DEFAULT_PROJECT["alerts"], self.cfg.get("alerts") or {})

    @property
    def abbreviation_files(self) -> list:
        files = self.cfg.get("abbreviations") or []
        files = files if isinstance(files, list) else [files]
        return [self.settings.resolve_path(f) for f in files]

    @property
    def venue_files(self) -> list:
        files = self.cfg.get("venues") or []
        files = files if isinstance(files, list) else [files]
        return [self.settings.resolve_path(f) for f in files]

    @cached_property
    def abbreviator(self):
        from litsuite.abbreviations import load_abbreviations

        return load_abbreviations(self.abbreviation_files, self.cfg.get("abbreviations_inline") or {})

    def venue_resolver(self, catalog_rows=None):
        from litsuite.venues import VenueResolver, learn_venue_codes, load_venue_files

        learned = learn_venue_codes(catalog_rows or [])
        return VenueResolver(load_venue_files(self.venue_files), learned,
                             self.cfg.get("venue_doi_styles") or {}, self.naming["preprint_label"])

    @property
    def recursive_setting(self):
        val = self.cfg.get("recursive", "ask")
        if isinstance(val, str):
            low = val.strip().lower()
            if low in ("true", "yes", "y", "1"):
                return True
            if low in ("false", "no", "n", "0"):
                return False
            return "ask"
        return bool(val)
