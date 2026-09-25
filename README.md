# Parliamentary Pulse

A small dashboard for live BBC Parliament subtitles. It shows the full captured transcript, a daily word cloud, cheap local topic signals, and word/topic occurrence over time.

## Run locally

Build data from an existing scraper session, then serve `dist/`:

```bash
python3 scripts/build_dashboard.py --session /path/to/session
python3 -m http.server 4173 --directory dist
```

Open <http://localhost:4173>.

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
