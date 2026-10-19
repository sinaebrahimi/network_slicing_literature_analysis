"""Data loading for litreview_dashboard.ipynb, so the notebook works for any project.

    from litsuite.dashboard import load_dashboard
    ctx = load_dashboard("network_slicing")
    df, FIG_DIR = ctx.df, ctx.figures_dir
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from litsuite.config import ConfigError, Settings

REPO = Path(__file__).resolve().parent.parent


@dataclass
class DashboardContext:
    project: str
    df: object
    catalog_path: Path
    figures_dir: Path
    compound_terms: dict = field(default_factory=dict)
    trend_topics: list = field(default_factory=list)
    journals: set = field(default_factory=set)
    conferences: set = field(default_factory=set)
    title: str = ""


def _settings_with_project(project: str, config=None) -> Settings:
    settings = Settings.load(config)
    if project in settings.project_names:
        return settings
    example = REPO / "config" / "settings.example.yaml"
    fallback = Settings.load(example)
    if project in fallback.project_names:
        return fallback
    raise ConfigError(f"Project '{project}' is not defined in {settings.path or 'config/settings.yaml'}")


def default_vocabulary(project) -> tuple:
    """Compound terms and trend topics built from the project's keywords and abbreviations."""
    kw = project.keywords
    topics = [k for k in (kw.get("focus") or []) + (kw.get("secondary") or []) if k]
    ab = project.abbreviator
    terms = {}
    for t in topics:
        variants = {t.lower(), ab.expand(t).lower(), ab.abbreviate(t).lower(), t.lower().replace("-", " ")}
        terms[t.lower()] = sorted(v for v in variants if v)
    return terms, [t.lower() for t in topics]


def load_dashboard(project: str, config=None) -> DashboardContext:
    import pandas as pd

    settings = _settings_with_project(project, config)
    prj = settings.project(project)
    catalog = prj.catalog_path
    if not catalog.exists():
        raise FileNotFoundError(f"No catalog yet for '{project}': {catalog}. Run: litsuite catalog create -p {project}")
    try:
        df = pd.read_csv(catalog, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(catalog, encoding="cp1252")
    df.columns = df.columns.astype(str).str.replace("﻿", "", regex=False).str.strip()
    if "status" in df.columns:
        df = df[df["status"].fillna("present") != "missing"].copy()
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")

    dash = prj.cfg.get("dashboard") or {}
    data = {}
    if dash.get("file"):
        data = yaml.safe_load(settings.resolve_path(dash["file"]).read_text(encoding="utf-8")) or {}
    data = {**data, **{k: v for k, v in dash.items() if k != "file"}}
    terms, topics = default_vocabulary(prj)
    figures = prj.data_dir / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    return DashboardContext(
        project=project, df=df, catalog_path=catalog, figures_dir=figures,
        compound_terms=data.get("compound_terms") or terms,
        trend_topics=data.get("trend_topics") or topics,
        journals=set(data.get("journals") or []), conferences=set(data.get("conferences") or []),
        title=prj.cfg.get("description") or project,
    )
