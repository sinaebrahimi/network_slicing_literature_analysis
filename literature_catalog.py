"""
Literature Catalog Builder (v1 entry point, kept for backward compatibility)
===========================================================================
This now runs `litsuite catalog update`, which creates the catalog the first
time and only adds new PDFs afterwards. Prefer the litsuite command directly:

    litsuite catalog create --folder "C:/path/to/Literature"        # ad-hoc
    litsuite catalog update --project network_slicing                # from settings.yaml

Usage (same as before, plus optional arguments):
    python literature_catalog.py                     # asks for the folder
    python literature_catalog.py --folder PATH --output litreview.csv --recursive
    LITERATURE_DIR=PATH python literature_catalog.py
"""

import os
import sys

from litsuite.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if os.environ.get("LITERATURE_DIR") and "--folder" not in args and "--project" not in args:
        args += ["--folder", os.environ["LITERATURE_DIR"]]
    raise SystemExit(main(["catalog", "update", *args]))
