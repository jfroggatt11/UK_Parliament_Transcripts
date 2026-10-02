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
root = Path(sys.argv[1]).resolve() / "bbc"
if not root.exists():
    raise SystemExit(f"BBC output directory was not created: {root}")
sessions = [p for p in root.iterdir() if p.is_dir() and (p / "session.json").exists()]
if not sessions:
    raise SystemExit("No BBC session was created")
print(max(sessions, key=lambda p: p.stat().st_mtime))
PY
)"

# The scraper writes subtitle cues incrementally under subs/. Build the
# transcript after capture so the dashboard can consume the completed session.
(cd "${SCRAPER_DIR}" && python3 -m src.transcript "${SESSION_DIR}")

python3 "${ROOT_DIR}/scripts/build_dashboard.py" \
  --session "${SESSION_DIR}" \
  --output "${ROOT_DIR}/dist/data/dashboard.json"
echo "Dashboard data refreshed from ${SESSION_DIR}"
