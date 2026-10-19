#!/usr/bin/env bash
# One-time setup on macOS / Linux:  bash scripts/install.sh
# Creates .venv, installs litsuite with every optional component, downloads the browser
# used for institutional downloads, and copies the example settings if you have none yet.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
python -m playwright install chromium
if [ ! -f config/settings.yaml ]; then
  cp config/settings.example.yaml config/settings.yaml
  echo "Created config/settings.yaml - edit it (or run: litsuite init)."
fi
echo
echo "Done. Activate the environment in each new terminal with:  source .venv/bin/activate"
echo "Then try:  litsuite doctor"
