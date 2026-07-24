#!/usr/bin/env bash
# Launch the seedlib browser. Reads tarballs in-place — no extraction needed
# (but they must be real LFS content: `git lfs pull` if they're pointer files).
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --with-requirements requirements.txt streamlit run app.py "$@"
