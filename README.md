# Parliamentary Pulse

A small dashboard for live BBC Parliament subtitles. It shows the full captured transcript, a daily word cloud, cheap local topic signals, and word/topic occurrence over time.

## Run locally

Build data from an existing scraper session, then serve `dist/`:

```bash
python3 scripts/build_dashboard.py --session /path/to/session
python3 -m http.server 4173 --directory dist
```

Open <http://localhost:4173>.

## Local live mode

Run the local continuous collector and dashboard with:

```bash
./scripts/run_local.sh --browser
```

Open <http://127.0.0.1:4173>. On the first run, Chromium opens BBC iPlayer. Sign in if prompted, start BBC Parliament, and turn subtitles on. The saved browser profile is kept under `vendor/Live_Subtitle_Scraper/.browser_profile/`; later runs can use `--browser --headless` after the first interactive setup.

The local app stores data in `work/local/pulse.sqlite3` (ignored by git). It exposes:

- live words at `/api/live`;
- today’s cloud and recurring phrases at `/api/analytics?days=7`;
- longer-running daily, hourly, and topic signals at `/api/analytics?days=30` or `days=90`;
- a health view at `/api/health`.

The core app uses Python’s standard library. The browser bootstrap requires the vendored scraper dependencies and Playwright Chromium:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install playwright
playwright install chromium
```

If you already have a completed scraper session, load it without contacting BBC:

```bash
./scripts/run_local.sh --no-capture --import-session /path/to/session
```

The dashboard then remains available locally while the imported history is analysed.

## Automated capture

The vendored scraper already includes the `bbc_parliament` channel entry in `src/config.py`. If you point `SCRAPER_DIR` at your original checkout, add this entry there:

```python
"bbc_parliament": "BBC Parliament",
```

The live iPlayer page is `https://www.bbc.co.uk/iplayer/live/bbcparliament`.

After adding that entry, run:

```bash
CAPTURE_MINUTES=12 ./scripts/run_pipeline.sh
```

The included GitHub Actions workflow runs the same pipeline hourly and commits the refreshed `dist/data/dashboard.json`. The analysis uses local lexical topic signals, so the refresh has no per-run model cost. A later upgrade can replace that step with local NMF/BERTopic or an API model without changing the dashboard contract.

## Parliament detection

The pipeline scores transcript text and channel metadata for UK Parliament, the Scottish Parliament, Senedd Cymru, the Northern Ireland Assembly, and the European Parliament. The dashboard shows the detected chamber and a confidence score; it is an inference layer, not an official programme label.
