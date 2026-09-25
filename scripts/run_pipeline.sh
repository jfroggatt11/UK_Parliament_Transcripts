#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRAPER_DIR="${SCRAPER_DIR:-${ROOT_DIR}/vendor/Live_Subtitle_Scraper}"
CAPTURE_MINUTES="${CAPTURE_MINUTES:-15}"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRAPER_DIR}/output}"

if [[ ! -f "${SCRAPER_DIR}/main.py" ]]; then
  echo "Scraper not found at ${SCRAPER_DIR}" >&2
  exit 1
fi

python3 "${SCRAPER_DIR}/main.py" \
  --channel bbc_parliament \
  --duration "${CAPTURE_MINUTES}" \
  --output-dir "${OUTPUT_DIR}"

SESSION_DIR="$(python3 - "${OUTPUT_DIR}" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1]) / "bbc"
sessions = [p.parent for p in root.rglob("transcript_chunks.txt")]
if not sessions:
    raise SystemExit("No completed BBC session found")
print(max(sessions, key=lambda p: p.stat().st_mtime))
PY
)"

python3 "${ROOT_DIR}/scripts/build_dashboard.py" \
  --session "${SESSION_DIR}" \
  --output "${ROOT_DIR}/dist/data/dashboard.json"
echo "Dashboard data refreshed from ${SESSION_DIR}"
