"""
Abstract Filler (v1 entry point, kept for backward compatibility)
=================================================================
Runs `litsuite abstracts fill`: rows with an empty abstract are looked up on
CrossRef, Semantic Scholar, OpenAlex and the publisher page; the catalog is
updated in place (a backup is kept), or written to --output.

    python fill_abstracts.py --project network_slicing
    python fill_abstracts.py --input litreview.csv --output litreview_filled.csv
"""

import sys

from litsuite.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["abstracts", "fill", *sys.argv[1:]]))
