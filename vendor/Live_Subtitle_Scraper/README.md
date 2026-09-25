# Live Subtitle Scraper

Harvest live subtitle segments from broadcasters and measure delivery latency in real time. The scraper supports BBC iPlayer, ITVX, Channel 4, Amazon Prime Video, and YouTube live streams, and can optionally record audio for ASR-based subtitle evaluation.

## Setup

Core scraper setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Optional extras for ASR evaluation:

- `ffmpeg` must be available on your `PATH` to decode DASH audio segments and browser recordings.
- `faster-whisper` and `numpy` are needed for `src.evaluate`.

```bash
pip install faster-whisper numpy
```

Notes:

- BBC auto mode uses Playwright Chromium.
- ITV, Channel 4, and Amazon auto mode require a local Google Chrome install because their live playback depends on Widevine DRM.
- YouTube mode uses `yt-dlp` (installed via `pip install -r requirements.txt`) — no browser required.

## Channel IDs

List them from the CLI at any time:

```bash
python main.py --list-channels
```

Supported channel IDs:

- BBC: `bbc_one_hd`, `bbc_one_london`, `bbc_one_scotland_hd`, `bbc_one_wales_hd`, `bbc_one_northern_ireland_hd`, `bbc_two_hd`, `bbc_two_northern_ireland_hd`, `bbc_two_wales_digital`, `bbc_news24`, `bbc_news_channel_hd`, `bbc_three_hd`
- ITV: `itv1`, `itv2`, `itv3`, `itv4`, `itvbe`, `citv`
- Channel 4: `channel4`, `e4`, `more4`, `film4`, `4seven`
- Amazon: `amazon_cnn`, `amazon_premier_league`, `amazon_nfl`, `amazon_champions_league`
- YouTube: pass `--youtube-url` instead of `--channel` (no fixed channel list)

## Common Workflows

### BBC: scrape, save audio, evaluate

```bash
python main.py --channel bbc_one_hd --auto --record-av --duration 10
```

Then evaluate:

```bash
SESSION=output/bbc/<session_id>
python -m src.transcript "$SESSION"
python -m src.evaluate "$SESSION" --model medium
```

Or automatically use the most recent session:

```bash
SESSION=$(ls -td output/bbc/* | head -n 1)
python -m src.evaluate "$SESSION" --model medium
```

### Amazon: scrape with DASH audio, evaluate

```bash
python main.py --channel amazon_cnn --auto --record-av --duration 10
```

With `--record-av`, the scraper fetches DASH audio segments alongside subtitles and saves them to `dash_audio/`. The evaluate step decodes these directly — no browser audio recording needed:

```bash
SESSION=$(ls -td output/amazon/* | head -n 1)
python -m src.evaluate "$SESSION" --model medium
```

### YouTube: scrape, save audio, evaluate

YouTube live streams require no browser session. Pass the watch URL directly — `yt-dlp` handles authentication automatically.

Captions only:

```bash
python main.py --youtube-url "https://www.youtube.com/watch?v=VIDEO_ID" --duration 60
```

Captions + audio (for ASR evaluation):

```bash
python main.py --youtube-url "https://www.youtube.com/watch?v=VIDEO_ID" \
               --duration 60 --record-av
```

With `--record-av`, the scraper polls the lowest-quality HLS video stream in parallel with captions, saving MPEG-TS segments to `audio_segments/`. At session end these are concatenated and decoded by `ffmpeg` to `audio.wav`. Timing is anchored via `#EXT-X-PROGRAM-DATE-TIME` — the same wall-clock reference used for caption timestamps — so no calibration beep or cross-correlation is needed.

Then evaluate:

```bash
SESSION=$(ls -td output/youtube/* | head -n 1)
python -m src.transcript "$SESSION"
python -m src.evaluate "$SESSION" --model medium
```

### Other examples

ITV:

```bash
python main.py --channel itv1 --auto --duration 10
```

Channel 4:

```bash
python main.py --channel channel4 --auto --duration 10
```

BBC manual mode:

```bash
python main.py --channel bbc_news24 --x-param 7 --duration 10
```

ITV/C4/Amazon manual mode with an MPD URL:

```bash
python main.py --channel itv1 --mpd-url "https://example.com/live.mpd" --duration 10
python main.py --channel amazon_cnn --mpd-url "https://example.com/cenc-sd.mpd" --duration 10
```

## Auto Mode Notes

BBC:

- Opens iPlayer in Playwright Chromium.
- Enable subtitles before capture completes.
- Login state is kept in `.browser_profile/`.

ITV:

- Opens ITVX in your system Chrome with a persistent profile.
- On first run, sign in and wait for playback to start.
- Captured MPD URLs include expiring auth tokens, so manual URLs need to be refreshed regularly.

Channel 4:

- Opens Channel 4 live TV in your system Chrome with a persistent profile.
- On first run, sign in and wait for playback to start.
- If you capture the MPD manually, use the content MPD rather than Yospace ad-insertion MPDs.

Amazon:

- Opens Amazon Prime Video in your system Chrome with a persistent profile.
- On first run, sign in and navigate to a live channel.
- The `?m=` manifest ID parameter and BaseURL are session-specific; refresh if requests fail.
- Subtitles are unencrypted TTML in MP4 containers despite the `cenc-` filename prefix.
- Use `--wait-for-enter` if you need to manually click into a stream before capture starts.

Important:

- For Amazon, subtitle text (`subs/`) and DASH audio segments (`dash_audio/`) are always saved.
- For YouTube, `--record-av` downloads MPEG-TS segments from the lowest-quality HLS stream in parallel with captions and decodes them to `audio.wav` at session end — no browser required.
- For BBC / ITV / Channel 4, `--record-av` records browser-tab audio into `audio.wav` — requires `--auto` to keep the browser open.
- For Amazon, subtitles and DASH audio are always saved; `--record-av` is not needed.
- `src.evaluate` requires `subs/` and one of: `audio.wav` (YouTube, BBC, ITV, C4) or `dash_audio/` (Amazon).

## CLI Options

| Flag | Scope | Description | Default |
|------|-------|-------------|---------|
| `--list-channels` | all | Print all supported channel IDs and exit. | off |
| `--broadcaster` | all | Broadcaster override. Usually auto-detected from `--channel`. | auto |
| `--channel` | BBC, ITV, C4, Amazon | Channel ID to scrape. | required (unless `--youtube-url`) |
| `--youtube-url` | YouTube | Full YouTube watch URL of a live stream. Replaces `--channel`. | none |
| `--duration` | all | Capture duration in minutes. | `60` |
| `--output-dir` | all | Root output directory. | `output/` |
| `--auto` | BBC, ITV, C4, Amazon | Open a browser and capture session parameters automatically. | off |
| `--headless` | auto | Run the browser headless. Use with `--auto`. | off |
| `--record-av` | all | Record audio for ASR evaluation. For YouTube: saves MPEG-TS segments → `audio.wav` (no browser needed). For BBC/ITV/C4: records browser-tab audio. Not needed for Amazon. | off |
| `--wait-for-enter` | Amazon | Pause before MPD capture and wait for Enter — useful to ensure the correct stream is selected. | off |
| `--mpd-url` | ITV, Channel 4, Amazon | MPD manifest URL for manual mode. | none |
| `--live-url` | Amazon | Override the Amazon live TV page URL opened by `--auto`. | none |
| `--base-url` | BBC | Override the BBC CDN base URL in manual mode. | broadcaster default |
| `--x-param` | BBC | Override BBC `x=` in manual mode. | `4` |
| `-v`, `--verbose` | all | Enable debug logging. | off |

## Output

Each run writes to `output/<broadcaster>/<session_id>/`:

```text
output/youtube/20260417_075159/
├── latency.csv
├── session.json
├── audio.wav                  # decoded 16kHz mono WAV (YouTube/BBC/ITV/C4 with --record-av)
├── audio_segments/            # raw MPEG-TS segments before decoding (YouTube only)
│   ├── 000006393.ts
│   └── ...
├── evaluation.json            # after python -m src.evaluate
├── transcript_words.txt       # after python -m src.transcript
├── transcript_chunks.txt      # after python -m src.transcript
└── subs/                      # subtitle cue text (one file per segment)
    ├── 6393.txt
    └── ...

output/amazon/20260404_164539/
├── latency.csv
├── session.json
├── debug_mpd.xml              # first MPD fetch saved for inspection
├── subs/
└── dash_audio/                # DASH audio segments (Amazon, always saved)
    ├── init.mp4
    ├── 89515636.471_44963514.m4s
    └── ...
```

Key files:

- `latency.csv`: per-cue latency records
- `session.json`: session metadata, timing anchors, and runtime config
- `subs/*.txt`: tab-separated `begin_unix`, `end_unix`, `text`
- `audio.wav`: 16kHz mono WAV for ASR (YouTube and BBC/ITV/C4 with `--record-av`)
- `audio_segments/`: raw MPEG-TS chunks saved during YouTube capture (decoded into `audio.wav` at session end)
- `dash_audio/`: DASH audio segments used by `src.evaluate` for timing-accurate ASR (Amazon)
- `evaluation.json`: ASR-vs-subtitle evaluation summary

## Analysis Pipeline

Build subtitle transcripts from saved subtitle cues:

```bash
python -m src.transcript output/bbc/<session_id>
python -m src.transcript output/youtube/<session_id>
```

This produces:

- `transcript_words.txt`: one word per line with a relative timestamp
- `transcript_chunks.txt`: sentence or phrase chunks with timestamp ranges

Evaluate subtitles against ASR:

```bash
python -m src.evaluate output/bbc/<session_id> --model medium
python -m src.evaluate output/youtube/<session_id> --model medium
python -m src.evaluate output/amazon/<session_id> --model medium
```

`src.evaluate` reports:

- word error rate (WER)
- subtitle coverage
- subtitle delay relative to detected speech
- SubLQ metrics from the Beyond Latency v2 framework

For YouTube sessions, `src.evaluate` uses `audio.wav` produced at session end. The timing anchor is derived from `#EXT-X-PROGRAM-DATE-TIME` — the same wall-clock reference as the caption timestamps — so no calibration beep or cross-correlation is needed.

For Amazon sessions, `src.evaluate` automatically uses saved DASH audio segments for ASR instead of browser-tab audio. The DASH segments carry exact DASH timestamps, so no anchor calibration is needed.

Supported Whisper model names: `tiny`, `base`, `small`, `medium`, `large-v3`.

## How Timing Works

### YouTube

YouTube live streams carry `#EXT-X-PROGRAM-DATE-TIME` (PDT) headers in their HLS playlists. Both the caption playlist and the video/audio playlist use the same wall-clock PDT base, so subtitle cue timestamps (`begin_unix`) and audio sample timestamps share a common reference without any browser mediation.

Caption timing:
- Cue relative timestamps (e.g. `00:00:02.500`) are offset by the segment's PDT → `begin_unix = pdt_unix + relative_seconds`

Audio timing (with `--record-av`):
- MPEG-TS segments are downloaded alongside captions and saved to `audio_segments/`.
- At session end, ffmpeg concatenates and decodes them to `audio.wav`.
- The PDT of the first audio segment (`audio_pdt_unix`) is recorded in `session.json` as `audio_start_unix - stream_latency`. ASR word timestamps are relative to `audio_pdt_unix`, which is the same timeline as `begin_unix` — no further calibration needed.

### BBC / ITV / Channel 4

Subtitle cue timestamps are absolute wall-clock (BBC) or derived from `availabilityStartTime + media_time`. The browser-tab recording is anchored to the same timeline via `video.currentTime` captured at recording start, corrected for a measured pipeline delay (a 6 kHz calibration tone is injected 2 seconds after recording begins and detected in the WAV).

### Amazon

Amazon DASH audio segments are fetched directly from the CDN alongside subtitle segments. Each segment has a known DASH presentation time in its filename (`<dash_time>_<id>.m4s`). During evaluation:

1. All segments are concatenated (init + media) and decoded to a WAV via ffmpeg.
2. ASR runs on that WAV. Word timestamps are relative to the first segment's DASH time.
3. Subtitle cue timestamps are also in the same DASH timeline (`AST + media_time`).
4. Delay = subtitle cue time − (AST + first_segment_dash_time + ASR word time).

This bypasses all browser timing uncertainty. The subtitle delay shown is the true production delay of the captions relative to the audio content.

## What The Metrics Mean

The live scraper records transport and packaging latency from the subtitle stream itself:

- `cdn_latency`: how far behind the media timeline the fetched subtitle segment is.
- `subtitle_video_offset`: subtitle cue time minus aligned video media time (BBC only).
- `edge_latency`: subtitle live-edge lag versus video live-edge (BBC only).

The ASR evaluation adds speech-to-subtitle quality metrics:

- WER and coverage from ASR/subtitle word alignment
- mean, median, standard deviation, and p95 subtitle delay
- SubLQ summary over aligned per-word delays

A positive subtitle delay means subtitles appear after the speech (the normal case for live captioning). A negative delay would mean subtitles appear before the speech, which typically indicates a timing anchor error.

## Broadcaster Differences

YouTube:

- No fixed channel registry — pass any YouTube live stream URL via `--youtube-url`.
- Auto-generated English captions delivered as segmented WebVTT in a signed DVR-mode HLS playlist; scraper jumps to the live edge on startup, ignoring DVR history.
- 5-second segments; `#EXT-X-PROGRAM-DATE-TIME` anchors both caption and audio timelines.
- `yt-dlp` resolves signed playlist URLs (expiry ~30 minutes); refreshed automatically.
- Audio: lowest-quality muxed HLS stream (typically 240p, ~200 kbps) polled in parallel; MPEG-TS segments are self-contained and decoded to `audio.wav` via ffmpeg with `-vn`.
- No edge latency measurement (would require comparing video and caption sequence numbers; not currently implemented).

BBC:

- Static segment numbering; subtitle segments can be fetched directly.
- TTML subtitles inside ISOBMFF `.m4s`.
- Word-level rolling cues.
- Can also probe matching video segments for video-aligned latency metrics.

ITV:

- Dynamic MPD manifest polled every 6 seconds.
- TTML subtitles inside ISOBMFF `.dash`.
- Phrase-level cues.
- MPD URLs are tokenised and expire.

Channel 4:

- Dynamic MPD manifest polled every 4 seconds.
- Plain WebVTT subtitle files.
- Phrase-level cues with absolute timing reconstructed from `availabilityStartTime`.

Amazon:

- Dynamic MPD manifest polled every 5 seconds.
- TTML subtitles inside ISOBMFF `.mp4` (timestamps offset by `availabilityStartTime`).
- DASH audio segments fetched alongside subtitles for accurate ASR evaluation.
- Word-level cues (17–433 ms) in US CEA-608 style (ALL CAPS, `>>` speaker change).
- Inline UTC timing from manifest `SupplementalProperty` instead of external Akamai.
- 2-second segments with 48,000 audio timescale and 90,000 subtitle timescale.

## Project Structure

```text
main.py
src/
├── browser.py          # Playwright-based browser automation for auto mode
├── clock.py            # Akamai/NTP wall-clock helpers
├── config.py           # Channel registry and SessionConfig
├── metrics.py          # SubLQ and latency metric calculations
├── storage.py          # Session logging and output file management
├── transcript.py       # Word/chunk transcript builder from subtitle cues
├── evaluate.py         # ASR alignment and subtitle delay evaluation
├── asr.py              # WhisperX transcription wrapper
├── parsers/
│   ├── isobmff.py      # MP4 box parser (tfdt extraction)
│   ├── mpd.py          # MPEG-DASH manifest parser
│   ├── ttml.py         # EBU-TT-D / TTML subtitle parser
│   └── webvtt.py       # WebVTT subtitle parser
└── scrapers/
    ├── base.py         # BaseScraper polling loop
    ├── bbc.py          # BBC iPlayer
    ├── itv.py          # ITVX
    ├── channel4.py     # Channel 4
    ├── amazon.py       # Amazon Prime Video
    └── youtube.py      # YouTube live streams (HLS WebVTT + MPEG-TS audio)
```
