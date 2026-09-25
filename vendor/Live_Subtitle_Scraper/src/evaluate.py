"""Evaluate subtitle quality by comparing ASR transcript against scraped subtitles.

Aligns ASR words (ground truth) with subtitle words, then computes:
  - WER (Word Error Rate)
  - Subtitle delay (time from speech to subtitle appearance)
  - Coverage (fraction of spoken words that appear in subtitles)
  - SubLQ composite score from the Beyond Latency v2 framework

Usage:
    python -m src.evaluate output/channel4/20260325_153839
    python -m src.evaluate output/channel4/20260325_153839 --model medium
"""

from __future__ import annotations

import csv
import json
import logging
import re
import statistics
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from src import metrics
from src.asr import AsrWord, transcribe
from src.transcript import load_cues, build_word_transcript

log = logging.getLogger(__name__)


# --- Data types ---

@dataclass(frozen=True, slots=True)
class SubWord:
    """A subtitle word with audio-relative timestamp."""
    audio_time: float  # seconds from audio start
    word: str


@dataclass(frozen=True, slots=True)
class AlignedPair:
    """An aligned ASR ↔ subtitle word pair."""
    asr_word: str | None
    sub_word: str | None
    asr_time: float | None
    sub_time: float | None
    kind: str  # 'match', 'substitution', 'insertion', 'deletion'


@dataclass(frozen=True, slots=True)
class AlignedWord:
    """A single aligned ASR↔subtitle word pair with timing and delay."""
    asr_time: float | None     # seconds from audio start (speech time)
    sub_time: float | None     # seconds from audio start (subtitle time)
    asr_word: str | None
    sub_word: str | None
    delay: float | None        # sub_time - asr_time + pipeline_offset
    kind: str                  # match, substitution, insertion, deletion


@dataclass(frozen=True, slots=True)
class DelayEvent:
    """A collapsed subtitle reveal event used for summary delay metrics."""
    asr_time: float
    sub_time: float
    raw_delay: float
    delay: float
    matched_words: int
    exact_matches: int


@dataclass
class EvalReport:
    """Full evaluation results."""
    wer: float
    num_asr_words: int
    num_sub_words: int
    matches: int
    substitutions: int
    insertions: int
    deletions: int
    coverage: float
    num_delay_events: int
    pipeline_offset: float     # browser pipeline offset applied (0 if none)
    median_raw_delay: float    # raw median before offset correction
    mean_delay: float | None   # after offset correction: 0 = in sync, +ve = subtitle lags
    median_delay: float | None
    p95_delay: float | None
    delay_std: float | None
    sublq: metrics.SubLQResult | None
    delays: list[float]        # event-level delays used for summary metrics
    delay_events: list[DelayEvent]
    aligned_words: list[AlignedWord]  # per-word alignment for plotting


# --- Text normalisation ---

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalise(word: str) -> str:
    """Lowercase, strip punctuation for matching."""
    return _PUNCT_RE.sub("", word.lower()).strip()


# --- Needleman-Wunsch alignment ---

def align_words(asr: list[str], sub: list[str]) -> list[tuple[int | None, int | None, str]]:
    """Global alignment of two word sequences using Needleman-Wunsch.

    Returns list of (asr_idx | None, sub_idx | None, kind) where kind is
    'match', 'substitution', 'insertion' (asr only), or 'deletion' (sub only).
    """
    n, m = len(asr), len(sub)
    MATCH, MISMATCH, GAP = 1, -1, -1

    # DP matrix
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + GAP
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + GAP

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            match_score = MATCH if asr[i - 1] == sub[j - 1] else MISMATCH
            dp[i][j] = max(
                dp[i - 1][j - 1] + match_score,
                dp[i - 1][j] + GAP,
                dp[i][j - 1] + GAP,
            )

    # Traceback
    alignment: list[tuple[int | None, int | None, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            match_score = MATCH if asr[i - 1] == sub[j - 1] else MISMATCH
            if dp[i][j] == dp[i - 1][j - 1] + match_score:
                kind = "match" if asr[i - 1] == sub[j - 1] else "substitution"
                alignment.append((i - 1, j - 1, kind))
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + GAP:
            alignment.append((i - 1, None, "insertion"))
            i -= 1
        else:
            alignment.append((None, j - 1, "deletion"))
            j -= 1

    alignment.reverse()
    return alignment


# --- Subtitle word loading ---

def _load_vtt_sidecar_cues(vtt_path: Path) -> list[tuple[float, float, str]]:
    """Parse a VTT sidecar file into (begin, end, text) tuples.

    Timestamps are video-relative seconds (0 = start of video content).
    Handles the rolling/progressive format C4 uses: consecutive cues build
    up word by word, so build_word_transcript will diff them correctly.
    """
    from src.parsers.webvtt import parse_webvtt
    text = vtt_path.read_text(encoding="utf-8", errors="replace")
    cues_raw = parse_webvtt(text, segment_anchor=0.0)
    return [(c.begin_unix, c.end_unix, c.text) for c in cues_raw]


def load_subtitle_words(session_dir: Path, session_start: float) -> list[SubWord]:
    """Load subtitle words with audio-relative timestamps.

    For live sessions loads from subs/*.txt; for VOD falls back to subtitles.vtt.
    """
    cues: list[tuple[float, float, str]] = []

    subs_dir = session_dir / "subs"
    if subs_dir.exists():
        cues = load_cues(subs_dir)

    if not cues:
        vtt_path = session_dir / "subtitles.vtt"
        if vtt_path.exists():
            log.info("Loading subtitles from VOD sidecar: %s", vtt_path.name)
            cues = _load_vtt_sidecar_cues(vtt_path)

    if not cues:
        return []

    word_ts = build_word_transcript(cues)

    words = []
    for video_time, word in word_ts:
        audio_time = video_time - session_start
        if audio_time >= 0:
            words.append(SubWord(audio_time=audio_time, word=word))

    return words


def _estimate_browser_buffer(session_dir: Path) -> float:
    """Estimate the browser-side buffer depth on top of CDN latency.

    When stream_latency is computed via tfdt_cdn_only, it only measures how
    stale the CDN segment was. The browser buffers additional content beyond
    the CDN edge. We estimate this from the gap between the CDN latency of
    the first subtitle segment and the stream_latency value — or fall back
    to a fixed 5s estimate.
    """
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text())
    stream_latency = meta.get("stream_latency")

    csv_path = session_dir / "latency.csv"
    if csv_path.exists() and stream_latency:
        try:
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cdn = row.get("cdn_latency", "")
                    if cdn:
                        first_cdn = float(cdn)
                        # stream_latency (CDN-only) should ≈ first segment CDN lag.
                        # Browser buffer is typically 3-8s on top.
                        # If they're close, use a 5s fixed estimate.
                        break
        except (ValueError, KeyError):
            pass

    return 5.0


def _estimate_buffer_delay(session_dir: Path) -> float | None:
    """Estimate stream buffer delay when session.json data is unavailable/unreliable.

    Uses the CDN latency of the first subtitle segment. At scraper startup, the
    first segment fetched is the most recent one on CDN. Its cdn_latency =
    fetch_time - segment_wall_clock tells us how old that segment was when fetched
    — i.e. the CDN's lag behind real-time. The browser adds additional buffer on
    top (typically 3-8s for traditional DASH). We add a 5s browser buffer estimate.

    This is an approximation. For reliable latency measurement, MSE timestampOffset
    data is needed.
    """
    csv_path = session_dir / "latency.csv"
    if not csv_path.exists():
        return None
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            first_segment_id = None
            first_cdn = None
            for row in reader:
                cdn = row.get("cdn_latency", "")
                if not cdn:
                    continue
                sid = row.get("segment_id", "")
                if first_segment_id is None:
                    first_segment_id = sid
                    first_cdn = float(cdn)
                    continue
                if sid != first_segment_id:
                    # We have the first segment's cdn_latency; stop
                    break
        if first_cdn is not None and first_cdn > 0:
            # subtitle CDN lag + browser buffer ahead of CDN delivery point.
            # The subtitle CDN lag (~7s) measures only CDN staleness; the browser
            # additionally buffers ~11s beyond that for smooth playback on Amazon.
            browser_buffer_estimate = 11.0
            estimated = first_cdn + browser_buffer_estimate
            log.info(
                "Estimated buffer: first segment cdn_latency=%.2fs + browser_buffer=%.1fs = %.2fs",
                first_cdn, browser_buffer_estimate, estimated,
            )
            return estimated
    except (ValueError, KeyError):
        pass
    return None


def _estimate_recent_subtitle_cdn_median(session_dir: Path) -> float | None:
    """Estimate the current subtitle CDN lag after audio recording starts.

    Some Amazon sessions begin with stale back-buffer subtitle segments and then
    jump to the currently playing period. In those cases the first segment in
    latency.csv is not representative. Using only rows at or after
    audio_start_unix gives a better estimate of the current subtitle stream.
    """
    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    audio_start = meta.get("audio_start_unix")
    if audio_start is None:
        return None

    csv_path = session_dir / "latency.csv"
    if not csv_path.exists():
        return None

    current_cdns: list[float] = []
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                cue_begin = row.get("cue_begin", "")
                cdn = row.get("cdn_latency", "")
                if not cue_begin or not cdn:
                    continue
                if float(cue_begin) >= float(audio_start):
                    current_cdns.append(float(cdn))
    except (ValueError, KeyError):
        return None

    if len(current_cdns) < 10:
        return None

    return statistics.median(current_cdns)


def _estimate_recent_subtitle_buffer_delay(
    session_dir: Path,
    browser_buffer_estimate: float = 16.0,
) -> float | None:
    """Estimate buffer delay from subtitle CDN lag after audio recording starts."""
    current_cdn = _estimate_recent_subtitle_cdn_median(session_dir)
    if current_cdn is None:
        return None

    estimated = current_cdn + browser_buffer_estimate
    log.info(
        "Estimated current buffer: subtitle cdn median after audio_start=%.2fs + "
        "browser_buffer=%.1fs = %.2fs",
        current_cdn, browser_buffer_estimate, estimated,
    )
    return estimated


def _looks_like_live_cc_subtitles(session_dir: Path) -> bool:
    """Heuristic for Amazon's live closed-caption style (ALL CAPS / >> markers)."""
    subs_dir = session_dir / "subs"
    if not subs_dir.exists():
        return False

    texts: list[str] = []
    for f in sorted(subs_dir.glob("*.txt"))[:8]:
        texts.append(f.read_text(encoding="utf-8", errors="ignore"))
    if not texts:
        return False

    blob = "\n".join(texts)
    upper = sum(1 for c in blob if c.isupper())
    lower = sum(1 for c in blob if c.islower())
    upper_ratio = upper / max(1, upper + lower)
    arrows = blob.count(">>")
    return upper_ratio > 0.8 or arrows >= 10


def _build_dash_audio_wav(session_dir: Path) -> tuple[Path, float] | None:
    """Concatenate saved DASH audio segments into a single WAV file.

    Returns (wav_path, first_dash_time) where first_dash_time is the DASH
    presentation time (seconds) of the first audio sample.  The ASR word
    timestamps from this WAV can be converted to wall-clock time by:
        word_wall_clock = AST + first_dash_time + word_time

    Returns None if no DASH audio segments are available.
    """
    if _dash_audio_is_encrypted(session_dir):
        log.info("Saved DASH audio is DRM-encrypted; skipping direct DASH decode")
        return None

    audio_dir = session_dir / "dash_audio"
    if not audio_dir.exists():
        return None

    init_path = audio_dir / "init.mp4"
    if not init_path.exists():
        log.warning("No DASH audio init.mp4 found")
        return None

    # Collect media segments, sorted by DASH time (encoded in filename)
    m4s_files = sorted(audio_dir.glob("*.m4s"))
    if not m4s_files:
        log.warning("No DASH audio segments (.m4s) found")
        return None

    # Parse first DASH time from filename: "89515636.471_12345.m4s"
    first_dash_time = float(m4s_files[0].stem.split("_")[0])

    # Concatenate init + all media segments into one file
    concat_path = audio_dir / "concat.mp4"
    with open(concat_path, "wb") as f:
        f.write(init_path.read_bytes())
        for m4s in m4s_files:
            f.write(m4s.read_bytes())

    # Decode to 16kHz mono WAV via ffmpeg
    wav_path = audio_dir / "dash_audio.wav"
    import subprocess
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(concat_path),
                "-ar", "16000", "-ac", "1",
                str(wav_path),
            ],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("ffmpeg decode of DASH audio failed: %s", result.stderr[-300:])
            return None
    except FileNotFoundError:
        log.warning("ffmpeg not found — cannot decode DASH audio")
        return None

    log.info(
        "Built DASH audio WAV from %d segments (first_dash_time=%.3f)",
        len(m4s_files), first_dash_time,
    )
    return wav_path, first_dash_time


def _load_dash_audio_meta(session_dir: Path) -> tuple[float, float] | None:
    """Load (AST, first_dash_time) from dash_audio/meta.json."""
    dash_meta_path = session_dir / "dash_audio" / "meta.json"
    if not dash_meta_path.exists():
        return None

    try:
        dash_meta = json.loads(dash_meta_path.read_text())
    except json.JSONDecodeError as exc:
        log.warning("Invalid dash_audio/meta.json: %s", exc)
        return None

    ast = dash_meta.get("ast")
    first_dash_time = dash_meta.get("first_dash_time")
    if ast is None or first_dash_time is None:
        log.warning("dash_audio/meta.json is missing AST or first_dash_time")
        return None
    return float(ast), float(first_dash_time)


def _dash_audio_is_encrypted(session_dir: Path) -> bool:
    """Return True when saved DASH audio is DRM-encrypted and not decodable."""
    dash_meta_path = session_dir / "dash_audio" / "meta.json"
    if not dash_meta_path.exists():
        dash_meta = {}
    else:
        try:
            dash_meta = json.loads(dash_meta_path.read_text())
        except json.JSONDecodeError:
            dash_meta = {}
    if dash_meta.get("encrypted") is not None:
        return bool(dash_meta.get("encrypted"))

    debug_mpd = session_dir / "debug_mpd.xml"
    if debug_mpd.exists():
        try:
            return "cenc_audio_" in debug_mpd.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
    return False


def _load_player_timing_override(session_dir: Path) -> dict[str, object] | None:
    """Load the best browser timing sample saved during capture.

    Policy matches the intended browser-side promotion:
    - use the start sample by default
    - override only if a later sample has a validated MSE timestampOffset
    """
    trace_path = session_dir / "player_timing.jsonl"
    if not trace_path.exists():
        return None

    best: dict[str, object] | None = None
    try:
        for raw in trace_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            sample = json.loads(raw)
            if sample.get("stream_latency") is None:
                continue
            if best is None:
                best = sample
                continue
            if sample.get("stream_latency_method") == "mse_timestamp_offset":
                best = sample
                break
    except (OSError, json.JSONDecodeError):
        return None
    return best


def _parse_dash_audio_segment_path(path: Path) -> tuple[float, str, str] | None:
    """Parse a saved DASH audio filename into (dash_time, group_id, segment_id)."""
    parts = path.stem.split("_")
    if len(parts) < 2:
        return None
    try:
        dash_time = float(parts[0])
    except ValueError:
        return None
    if len(parts) == 2:
        return dash_time, "default", parts[1]
    return dash_time, "_".join(parts[1:-1]), parts[-1]


def _collect_dash_audio_inputs(
    session_dir: Path,
) -> tuple[float, dict[str, Path], dict[str, list[tuple[float, str, Path]]]] | None:
    """Collect init files and saved DASH audio segments grouped by source."""
    meta = _load_dash_audio_meta(session_dir)
    if meta is None:
        return None
    ast, _ = meta

    audio_dir = session_dir / "dash_audio"
    init_files: dict[str, Path] = {}
    legacy_init = audio_dir / "init.mp4"
    if legacy_init.exists():
        init_files["default"] = legacy_init
    for init_path in audio_dir.glob("init_*.mp4"):
        init_files[init_path.stem[len("init_"):]] = init_path

    grouped: dict[str, list[tuple[float, str, Path]]] = {}
    for seg_path in sorted(audio_dir.glob("*.m4s")):
        parsed = _parse_dash_audio_segment_path(seg_path)
        if parsed is None:
            continue
        dash_time, group_id, segment_id = parsed
        grouped.setdefault(group_id, []).append((dash_time, segment_id, seg_path))

    if not grouped:
        return None
    return ast, init_files, grouped


def _decode_dash_audio_concat(
    init_path: Path,
    segment_paths: list[Path],
    wav_path: Path,
    expected_duration_s: float,
) -> bool:
    """Decode one contiguous DASH audio run to WAV."""
    concat_path = wav_path.with_suffix(".mp4")
    with open(concat_path, "wb") as f:
        f.write(init_path.read_bytes())
        for seg_path in segment_paths:
            f.write(seg_path.read_bytes())

    import subprocess
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(concat_path),
                "-ar", "16000", "-ac", "1",
                str(wav_path),
            ],
            capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        log.warning("ffmpeg not found — cannot decode DASH audio")
        return False
    finally:
        concat_path.unlink(missing_ok=True)

    if result.returncode != 0:
        wav_path.unlink(missing_ok=True)
        return False
    if not wav_path.exists() or wav_path.stat().st_size < 4096:
        wav_path.unlink(missing_ok=True)
        return False
    try:
        import wave

        with wave.open(str(wav_path), "rb") as wf:
            decoded_duration_s = wf.getnframes() / wf.getframerate()
    except Exception:
        wav_path.unlink(missing_ok=True)
        return False

    min_expected_duration_s = max(0.5, expected_duration_s * 0.5)
    if decoded_duration_s < min_expected_duration_s:
        log.warning(
            "Decoded DASH audio run is too short: got %.2fs from a %.2fs chunk span",
            decoded_duration_s, expected_duration_s,
        )
        wav_path.unlink(missing_ok=True)
        return False
    return True


def _decode_dash_audio_run(
    init_path: Path,
    chunks: list[tuple[float, str, Path]],
    output_dir: Path,
    run_index: int,
) -> list[tuple[Path, float]]:
    """Decode a contiguous run, recursively splitting around corrupt segments."""
    if not chunks:
        return []

    start_dash = chunks[0][0]
    end_dash = chunks[-1][0]
    expected_duration_s = max(2.0, (end_dash - start_dash) + 2.0)
    wav_path = output_dir / f"run_{run_index:03d}_{start_dash:.3f}_{end_dash:.3f}.wav"

    if _decode_dash_audio_concat(
        init_path,
        [p for _, _, p in chunks],
        wav_path,
        expected_duration_s,
    ):
        return [(wav_path, start_dash)]

    if len(chunks) == 1:
        log.debug("Dropping undecodable DASH audio segment %s", chunks[0][2].name)
        return []

    mid = len(chunks) // 2
    left = _decode_dash_audio_run(init_path, chunks[:mid], output_dir, run_index * 2)
    right = _decode_dash_audio_run(init_path, chunks[mid:], output_dir, run_index * 2 + 1)
    return left + right


def _build_dash_audio_runs(
    session_dir: Path,
) -> tuple[float, list[tuple[Path, float]]] | None:
    """Decode DASH audio into one or more absolute-time runs.

    Returns (audio_media_start_unix, runs) where each run is
    (wav_path, run_start_unix).
    """
    if _dash_audio_is_encrypted(session_dir):
        log.info("Saved DASH audio is DRM-encrypted; skipping DASH run decoding")
        return None

    collected = _collect_dash_audio_inputs(session_dir)
    if collected is None:
        return None
    ast, init_files, grouped = collected

    output_dir = session_dir / "dash_audio" / "decoded_runs"
    output_dir.mkdir(exist_ok=True)

    runs: list[tuple[Path, float]] = []
    run_index = 1
    for group_id, chunks in sorted(grouped.items()):
        init_path = init_files.get(group_id) or init_files.get("default")
        if init_path is None:
            log.warning("No init segment found for DASH audio group %s", group_id)
            continue

        chunks.sort(key=lambda item: item[0])
        current_run = [chunks[0]]
        for chunk in chunks[1:]:
            if chunk[0] - current_run[-1][0] > 2.5:
                for wav_path, run_start_dash in _decode_dash_audio_run(
                    init_path, current_run, output_dir, run_index,
                ):
                    runs.append((wav_path, ast + run_start_dash))
                    run_index += 1
                current_run = [chunk]
            else:
                current_run.append(chunk)

        for wav_path, run_start_dash in _decode_dash_audio_run(
            init_path, current_run, output_dir, run_index,
        ):
            runs.append((wav_path, ast + run_start_dash))
            run_index += 1

    if not runs:
        log.warning("Could not decode any DASH audio runs from %s", session_dir / "dash_audio")
        return None

    runs.sort(key=lambda item: item[1])
    audio_media_start = runs[0][1]
    log.info(
        "Built %d DASH audio run(s); anchor=%.3f",
        len(runs), audio_media_start,
    )
    return audio_media_start, runs


def _xcorr_stream_latency(session_dir: Path) -> float | None:
    """Compute stream latency by cross-correlating DASH reference audio with browser audio.

    The DASH audio segments have known absolute timestamps (AST + dash_time).
    The browser-recorded audio.wav starts at audio_start_unix.  Cross-correlating
    the two finds the sample offset L such that:

        stream_latency = audio_start_unix - T_dash + L / sample_rate

    where T_dash = AST + first_segment_dash_time.

    Returns latency in seconds, or None if unavailable / implausible.
    """
    if _dash_audio_is_encrypted(session_dir):
        return None

    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text())
    audio_start_unix = meta.get("audio_start_unix")
    if not audio_start_unix:
        return None

    # Load DASH timeline anchor from dash_audio/meta.json
    dash_meta_path = session_dir / "dash_audio" / "meta.json"
    if not dash_meta_path.exists():
        log.debug("No dash_audio/meta.json — xcorr unavailable")
        return None
    dash_meta = json.loads(dash_meta_path.read_text())
    ast = dash_meta.get("ast")
    first_dash_time = dash_meta.get("first_dash_time")
    if ast is None or first_dash_time is None:
        return None
    T_dash = ast + first_dash_time

    # Build DASH audio WAV from saved .m4s segments
    result = _build_dash_audio_wav(session_dir)
    if result is None:
        return None
    dash_wav_path, _ = result

    try:
        import numpy as np
        import wave

        def _load_wav(path: Path):
            with wave.open(str(path), "rb") as wf:
                sr = wf.getframerate()
                n_ch = wf.getnchannels()
                raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if n_ch == 2:
                samples = samples.reshape(-1, 2).mean(axis=1)
            return samples, sr

        audio_samples, sr_a = _load_wav(session_dir / "audio.wav")
        dash_samples, sr_d = _load_wav(dash_wav_path)

        if sr_a != sr_d:
            log.warning("Sample rate mismatch: audio=%d Hz, dash=%d Hz — resampling not implemented", sr_a, sr_d)
            return None
        sr = sr_a

        # Use the first 60s of browser audio for the search window.
        # Longer excerpts improve SNR but we cap to keep FFT fast.
        excerpt_len = min(len(audio_samples), int(60 * sr))
        audio_excerpt = audio_samples[:excerpt_len]

        # Cross-correlate: corr peak at lag L (samples) means
        #   audio_excerpt[n] ≈ dash_samples[n - L]
        # i.e. the same content appears L samples later in audio than in dash.
        # stream_latency = audio_start_unix - T_dash + L/sr
        from scipy.signal import correlate, correlation_lags
        corr = correlate(audio_excerpt, dash_samples, mode="full", method="fft")
        lags = correlation_lags(len(audio_excerpt), len(dash_samples), mode="full")
        peak_idx = int(np.argmax(corr))
        L = int(lags[peak_idx])
        lag_s = L / sr

        stream_latency = float(audio_start_unix) - T_dash + lag_s

        log.info(
            "XCorr: audio_start=%.3f T_dash=%.3f lag=%.2fs → stream_latency=%.2fs",
            float(audio_start_unix), T_dash, lag_s, stream_latency,
        )

        if not (3.0 < stream_latency < 90.0):
            log.warning(
                "XCorr stream_latency=%.1fs outside plausible range (3–90s) — rejecting",
                stream_latency,
            )
            return None

        return stream_latency

    except ImportError as exc:
        log.warning("scipy/numpy not available for xcorr: %s", exc)
        return None
    except Exception as exc:
        log.warning("XCorr stream latency failed: %s", exc)
        return None


def load_audio_media_start(session_dir: Path) -> float:
    """Get the media Unix timestamp corresponding to audio t=0.

    The browser plays media with a buffer delay, so audio t=0 corresponds
    to a media time slightly in the past. We compute this from:
        media_start = availabilityStartTime + video.currentTime_at_recording_start

    This anchors the audio timeline to the same media timeline that subtitle
    cue timestamps use, so delays are accurate for both live and pre-recorded.

    Falls back to audio_start_unix or session start_time if the precise
    video timing data is not available.
    """
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text())

    timing_override = _load_player_timing_override(session_dir)
    if timing_override and meta.get("stream_latency_method") != "mse_timestamp_offset":
        if timing_override.get("stream_latency_method") == "mse_timestamp_offset":
            log.info(
                "Using player_timing trace override: %s from note=%s",
                timing_override.get("stream_latency_method"),
                timing_override.get("note"),
            )
            meta["stream_latency"] = timing_override.get("stream_latency")
            meta["stream_latency_method"] = timing_override.get("stream_latency_method")
            meta["video_time_at_start"] = timing_override.get("video_time_at_start")
            meta["video_buffered_end"] = timing_override.get("video_buffered_end")
            meta["mse_timestamp_offset"] = timing_override.get("mse_timestamp_offset")
            meta["video_source_frame"] = timing_override.get("video_source_frame")
        elif timing_override.get("note") == "start":
            log.info(
                "Using player_timing start sample override: %s",
                timing_override.get("stream_latency_method"),
            )
            meta["stream_latency"] = timing_override.get("stream_latency")
            meta["stream_latency_method"] = timing_override.get("stream_latency_method")
            if timing_override.get("video_time_at_start") is not None:
                meta["video_time_at_start"] = timing_override.get("video_time_at_start")
            if timing_override.get("video_buffered_end") is not None:
                meta["video_buffered_end"] = timing_override.get("video_buffered_end")
            if timing_override.get("video_source_frame"):
                meta["video_source_frame"] = timing_override.get("video_source_frame")

    avail_start = meta.get("availability_start_time")
    video_time = meta.get("video_time_at_start")
    stream_latency = meta.get("stream_latency")

    # Best: audio cross-correlation with DASH reference segments (ground truth).
    # Measures actual playback latency independent of browser internals.
    xcorr_latency = _xcorr_stream_latency(session_dir)
    if xcorr_latency is not None and meta.get("audio_start_unix"):
        anchor = float(meta["audio_start_unix"]) - xcorr_latency
        log.info(
            "Audio media anchor from xcorr: audio_start(%.3f) - %.2fs = %.3f",
            float(meta["audio_start_unix"]), xcorr_latency, anchor,
        )
        return anchor

    # VOD path: subtitles.vtt timestamps are 0-based video time.
    # audio_time = vtt_ts - video_time_at_start, so the anchor IS video_time_at_start.
    if (session_dir / "subtitles.vtt").exists() and video_time is not None:
        anchor = float(video_time)
        log.info("VOD anchor from video_time_at_start: %.3f", anchor)
        return anchor

    # --- Fallback: stream_latency from session.json ---
    # "mse_timestamp_offset" means we had a validated vct+tsOffset pair in the browser.
    if stream_latency is not None and meta.get("audio_start_unix"):
        sl = float(stream_latency)
        method = meta.get("stream_latency_method", "unknown")
        recent_cdn = _estimate_recent_subtitle_cdn_median(session_dir)
        recent_buffer = _estimate_recent_subtitle_buffer_delay(session_dir)
        live_cc_style = (
            meta.get("broadcaster") == "amazon"
            and _looks_like_live_cc_subtitles(session_dir)
        )
        if method == "mse_timestamp_offset" and 0 < sl < 60:
            buffer_est = _estimate_buffer_delay(session_dir)
            if buffer_est is not None and sl < buffer_est - 6.0:
                log.warning(
                    "stream_latency=%.2fs from mse_timestamp_offset is much smaller than "
                    "subtitle-side buffer estimate %.2fs; likely wrong player timing. "
                    "Using the buffer estimate instead.",
                    sl, buffer_est,
                )
                anchor = float(meta["audio_start_unix"]) - buffer_est
                log.info(
                    "Audio media anchor from subtitle buffer estimate: audio_start(%.3f) - %.2fs = %.3f",
                    float(meta["audio_start_unix"]), buffer_est, anchor,
                )
                return anchor
            anchor = float(meta["audio_start_unix"]) - sl
            log.info(
                "Audio media anchor from stream_latency (%s): audio_start(%.3f) - %.2fs = %.3f",
                method, float(meta["audio_start_unix"]), sl, anchor,
                )
            return anchor
        if (
            method == "tfdt_cdn_only"
            and meta.get("broadcaster") == "amazon"
            and 0 <= sl < 3.0
        ):
            sl_adjusted = max(
                sl + 17.0,
                recent_buffer if recent_buffer is not None else 0.0,
            )
            anchor = float(meta["audio_start_unix"]) - sl_adjusted
            log.info(
                "Audio media anchor from stream_latency (%s, near-live CDN + Amazon player buffer): "
                "audio_start(%.3f) - %.2fs = %.3f",
                method, float(meta["audio_start_unix"]), sl_adjusted, anchor,
            )
            return anchor
        if method == "mse_timestamp_offset" and not (0 < sl < 60):
            log.warning(
                "stream_latency=%.1fs from mse_timestamp_offset is outside plausible "
                "range (0–60s) — likely mismatched video/MSE capture; skipping",
                sl,
            )
        elif sl >= 3.0 and abs(sl) < 300:
            if method == "tfdt_cdn_only":
                # CDN-only: stream_latency = request_time - segment_wall_clock.
                # This is only the CDN's staleness; the browser buffers an additional
                # ~5s on top. Add an estimate so the anchor isn't too late.
                browser_buffer_est = _estimate_browser_buffer(session_dir)
                sl_adjusted = sl + browser_buffer_est
                if live_cc_style and recent_cdn is not None:
                    live_cc_buffer_est = recent_cdn + 16.5
                    if sl_adjusted > live_cc_buffer_est + 1.5:
                        log.info(
                            "Amazon live-CC anchor: tfdt_cdn_only + %.1fs browser buffer "
                            "implies %.2fs of lag, but current subtitle-side estimate is "
                            "%.2fs. Using the subtitle-side live-CC estimate instead.",
                            browser_buffer_est, sl_adjusted, live_cc_buffer_est,
                        )
                        sl_adjusted = live_cc_buffer_est
                anchor = float(meta["audio_start_unix"]) - sl_adjusted
                log.info(
                    "Audio media anchor from stream_latency (CDN-only + %.1fs browser buffer est): "
                    "audio_start(%.3f) - %.2fs = %.3f",
                    browser_buffer_est, float(meta["audio_start_unix"]), sl_adjusted, anchor,
                )
            else:
                anchor = float(meta["audio_start_unix"]) - sl
                log.info(
                    "Audio media anchor from stream_latency (%s): audio_start(%.3f) - %.2fs = %.3f",
                    method, float(meta["audio_start_unix"]), sl, anchor,
                )
            return anchor
        elif method != "mse_timestamp_offset":
            log.warning(
                "stream_latency=%.3fs rejected (outside 3–300s range) — "
                "will use fallback anchor", sl,
            )

    # --- Next: availabilityStartTime + video.currentTime ---
    # Valid for ITV, C4, and Amazon when vct is from the actual player (not
    # a preview thumbnail).  Reject if AST+vct is too close to live edge
    # (indicates preview video) or years off (broken capture).
    if avail_start is not None and video_time is not None:
        media_start = float(avail_start) + float(video_time)
        audio_start = float(meta["audio_start_unix"]) if meta.get("audio_start_unix") else None
        session_start_dt = datetime.fromisoformat(meta["start_time"])
        session_start_unix = session_start_dt.timestamp()
        drift = abs(media_start - session_start_unix)
        ahead_of_recording = (audio_start is not None and media_start > audio_start - 3.0)
        source_frame = str(meta.get("video_source_frame") or "")
        recent_buffer = _estimate_recent_subtitle_buffer_delay(session_dir)
        if (
            audio_start is not None
            and recent_buffer is not None
            and "(CDP)" not in source_frame
        ):
            implied_latency = audio_start - media_start
            if implied_latency > recent_buffer + 30.0:
                anchor = audio_start - recent_buffer
                log.warning(
                    "AST + vct implies %.2fs of player lag from an unvalidated DOM video "
                    "element, but current subtitle-side estimate is %.2fs. "
                    "Using the subtitle-side estimate instead.",
                    implied_latency, recent_buffer,
                )
                log.info(
                    "Audio media anchor from current subtitle buffer estimate: "
                    "audio_start(%.3f) - %.2fs = %.3f",
                    audio_start, recent_buffer, anchor,
                )
                return anchor
        if drift < 3600 and not ahead_of_recording:
            log.info(
                "Audio media anchor: AST(%.3f) + vct(%.3f) = %.3f",
                avail_start, video_time, media_start,
            )
            return media_start
        else:
            reason = "too close to live edge" if ahead_of_recording else f"{drift:.0f}s from session start"
            log.warning(
                "AST + vct = %.3f rejected (%s) — estimating buffer from latency data",
                media_start, reason,
            )
            buffer = _estimate_buffer_delay(session_dir)
            if buffer is not None and meta.get("audio_start_unix"):
                anchor = float(meta["audio_start_unix"]) - buffer
                log.info(
                    "Estimated buffer delay: %.1fs → anchor = %.3f",
                    buffer, anchor,
                )
                return anchor

    # BBC live: video.currentTime is already Unix-scale (very large number)
    # because BBC encodes absolute time in the media timeline
    if video_time is not None and float(video_time) > 1_000_000_000:
        log.info(
            "Audio media anchor: video.currentTime is Unix-scale (%.3f)",
            float(video_time),
        )
        return float(video_time)

    # Fallback: wall clock time (includes stream buffer offset)
    if meta.get("audio_start_unix"):
        log.warning(
            "No video_time_at_start — using wall clock (delays will include stream buffer)"
        )
        return float(meta["audio_start_unix"])

    log.warning("No audio timing data — falling back to session start_time")
    dt = datetime.fromisoformat(meta["start_time"])
    return dt.timestamp()


# --- Main evaluation ---

def _detect_calibration_tone(wav_path: Path, expected_offset: float) -> float | None:
    """Detect a 6kHz calibration tone in the audio and return its timestamp.

    The tone was injected at a known wall-clock offset from recording start.
    We search a window around the expected position using FFT energy in the
    6kHz band (bin closest to 6000Hz in a short-window FFT).

    Returns the detected tone timestamp in seconds, or None if not found.
    """
    import numpy as np
    import wave

    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # Search window: expected_offset ± 3 seconds
    search_start = max(0, int((expected_offset - 3) * sr))
    search_end = min(len(samples), int((expected_offset + 3) * sr))
    segment = samples[search_start:search_end]

    # Short-window FFT to find 6kHz energy peaks
    win_size = 1024  # ~64ms at 16kHz
    hop = 256
    target_freq = 6000
    freq_bin = int(target_freq * win_size / sr)

    energies = []
    for i in range(0, len(segment) - win_size, hop):
        window = segment[i:i + win_size]
        fft = np.abs(np.fft.rfft(window))
        # Energy in the 6kHz band (±2 bins)
        lo = max(0, freq_bin - 2)
        hi = min(len(fft), freq_bin + 3)
        energy = np.mean(fft[lo:hi])
        energies.append((i, energy))

    if not energies:
        return None

    # Find the peak energy frame
    max_idx, max_energy = max(energies, key=lambda x: x[1])
    median_energy = np.median([e for _, e in energies])

    # The tone should be significantly above the background
    if max_energy < median_energy * 3:
        log.warning("Calibration tone not detected (peak=%.2f, median=%.2f)", max_energy, median_energy)
        return None

    # Convert back to absolute time in the audio
    tone_time = (search_start + max_idx) / sr
    log.info(
        "Calibration tone detected at %.3fs (expected ~%.3fs, energy ratio=%.1fx)",
        tone_time, expected_offset, max_energy / median_energy,
    )
    return tone_time


def _calibrate_pipeline_delay(session_dir: Path) -> float:
    """Calibrate audio pipeline delay using the injected calibration tone.

    A 6kHz tone was played at a known wall-clock offset from recording start.
    We detect it in the audio to measure the MediaRecorder pipeline delay:
        pipeline_delay = tone_audio_time - tone_wall_offset

    Positive = audio is captured late (MediaRecorder lags wall clock).
    """
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text())
    beep_offset = meta.get("calibration_beep_offset")
    if beep_offset is None:
        log.warning("No calibration beep offset — pipeline delay unknown")
        return 0.0

    wav_path = session_dir / "audio.wav"
    tone_time = _detect_calibration_tone(wav_path, float(beep_offset))
    if tone_time is None:
        return 0.0

    pipeline_delay = tone_time - float(beep_offset)
    log.info(
        "Pipeline delay: tone at %.3fs, expected at %.3fs → delay = %.3fs",
        tone_time, float(beep_offset), pipeline_delay,
    )
    return pipeline_delay


def _build_delay_events(aligned_words: list[AlignedWord]) -> list[DelayEvent]:
    """Collapse simultaneous subtitle word reveals into one summary event.

    When multiple new words appear at the same subtitle timestamp, using every
    word as an independent delay sample over-weights chunked subtitles. For
    session-level latency we instead keep one sample per subtitle reveal and
    anchor it to the least-lagging aligned word in that reveal.
    """
    grouped: dict[float, list[AlignedWord]] = {}
    ordered_keys: list[float] = []

    for word in aligned_words:
        if (
            word.delay is None
            or word.sub_time is None
            or word.asr_time is None
            or word.kind not in ("match", "substitution")
        ):
            continue
        key = round(word.sub_time, 6)
        if key not in grouped:
            grouped[key] = []
            ordered_keys.append(key)
        grouped[key].append(word)

    events: list[DelayEvent] = []
    for key in ordered_keys:
        group = grouped[key]
        matches = [word for word in group if word.kind == "match"]
        candidates = matches or group
        chosen = min(candidates, key=lambda word: word.delay if word.delay is not None else float("inf"))
        events.append(DelayEvent(
            asr_time=chosen.asr_time,
            sub_time=chosen.sub_time,
            raw_delay=chosen.sub_time - chosen.asr_time,
            delay=chosen.delay,
            matched_words=len(group),
            exact_matches=len(matches),
        ))

    return events


def _load_or_transcribe_dash_runs(
    session_dir: Path,
    dash_runs: list[tuple[Path, float]],
    audio_media_start: float,
    model_size: str,
    engine: str,
    rerun_asr: bool,
) -> list[AsrWord]:
    """Load cached DASH-run ASR or transcribe each run and shift to a common anchor."""
    asr_cache = session_dir / "asr_words_dash.json"
    if not rerun_asr and asr_cache.exists():
        log.info("Loading cached DASH ASR transcript from %s", asr_cache.name)
        cached = json.loads(asr_cache.read_text())
        return [AsrWord(start=w["start"], end=w["end"], word=w["word"]) for w in cached]

    words: list[AsrWord] = []
    for wav_path, run_start_unix in dash_runs:
        run_words = transcribe(wav_path, model_size, engine=engine)
        run_offset = run_start_unix - audio_media_start
        words.extend(
            AsrWord(
                start=w.start + run_offset,
                end=w.end + run_offset,
                word=w.word,
            )
            for w in run_words
        )

    words.sort(key=lambda w: w.start)
    if words:
        asr_cache.write_text(json.dumps(
            [{"start": w.start, "end": w.end, "word": w.word} for w in words],
            indent=None,
        ))
        log.info("Saved DASH ASR transcript to %s", asr_cache.name)
    return words


def _load_or_transcribe_browser_audio(
    session_dir: Path,
    model_size: str,
    engine: str,
    rerun_asr: bool,
) -> tuple[float, list[AsrWord]]:
    """Load/transcribe browser-recorded audio.wav using the session anchor logic."""
    wav_path = session_dir / "audio.wav"
    if not wav_path.exists():
        raise FileNotFoundError(
            f"No usable audio source in {session_dir} "
            f"(expected dash_audio/*.m4s or audio.wav)"
        )

    audio_media_start = load_audio_media_start(session_dir)
    asr_cache = session_dir / "asr_words.json"

    if not rerun_asr and asr_cache.exists():
        log.info("Loading cached ASR transcript from %s", asr_cache.name)
        cached = json.loads(asr_cache.read_text())
        asr_words = [AsrWord(start=w["start"], end=w["end"], word=w["word"]) for w in cached]
        log.info("Loaded %d cached ASR words", len(asr_words))
        return audio_media_start, asr_words

    asr_words = transcribe(wav_path, model_size, engine=engine)
    if asr_words:
        asr_cache.write_text(json.dumps(
            [{"start": w.start, "end": w.end, "word": w.word} for w in asr_words],
            indent=None,
        ))
        log.info("Saved ASR transcript to %s", asr_cache.name)
    return audio_media_start, asr_words


def evaluate(
    session_dir: Path,
    model_size: str = "base",
    pipeline_offset: float | None = None,
    engine: str = "whisperx",
    rerun_asr: bool = False,
) -> EvalReport:
    """Run full evaluation: ASR → align → metrics.

    The pipeline_offset corrects for the browser's audio pipeline delay —
    the gap between wall clock time and what MediaRecorder actually captures.
    If not provided, calibrates automatically by matching subtitle DOM change
    events (observed during recording) against ASR word timestamps.
    """
    dash_audio = _build_dash_audio_runs(session_dir)
    using_dash_audio = dash_audio is not None
    if using_dash_audio:
        audio_media_start, dash_runs = dash_audio
        log.info("Using %d DASH audio run(s) for evaluation", len(dash_runs))
        asr_words = _load_or_transcribe_dash_runs(
            session_dir,
            dash_runs,
            audio_media_start,
            model_size,
            engine,
            rerun_asr,
        )
        if not asr_words and (session_dir / "audio.wav").exists():
            log.warning(
                "DASH audio produced no ASR words; falling back to browser audio.wav"
            )
            using_dash_audio = False
            audio_media_start, asr_words = _load_or_transcribe_browser_audio(
                session_dir,
                model_size,
                engine,
                rerun_asr,
            )
    else:
        audio_media_start, asr_words = _load_or_transcribe_browser_audio(
            session_dir,
            model_size,
            engine,
            rerun_asr,
        )
    if not asr_words:
        raise RuntimeError("ASR produced no words")

    # 2. Decide whether a browser-pipeline correction is needed.
    if using_dash_audio:
        if pipeline_offset is None:
            pipeline_offset = 0.0
        elif pipeline_offset != 0.0:
            log.warning(
                "Applying a manual pipeline offset of %.3fs to DASH audio. "
                "This is usually unnecessary because DASH audio and subtitles "
                "already share the same media timeline.",
                pipeline_offset,
            )
    elif pipeline_offset is None:
        pipeline_offset = _calibrate_pipeline_delay(session_dir)

    # 3. Load subtitle words using the chosen media-time anchor.
    sub_words = load_subtitle_words(session_dir, audio_media_start)
    if not sub_words:
        raise RuntimeError("No subtitle words found")

    # 4. Normalise for alignment
    asr_norm = [_normalise(w.word) for w in asr_words]
    sub_norm = [_normalise(w.word) for w in sub_words]

    # Filter empty strings from normalisation
    asr_valid = [(i, n) for i, n in enumerate(asr_norm) if n]
    sub_valid = [(i, n) for i, n in enumerate(sub_norm) if n]

    asr_filtered = [n for _, n in asr_valid]
    sub_filtered = [n for _, n in sub_valid]
    asr_idx_map = [i for i, _ in asr_valid]
    sub_idx_map = [i for i, _ in sub_valid]

    log.info("Aligning %d ASR words with %d subtitle words...", len(asr_filtered), len(sub_filtered))
    raw_alignment = align_words(asr_filtered, sub_filtered)

    # Map back to original indices
    alignment = []
    for a_idx, s_idx, kind in raw_alignment:
        orig_a = asr_idx_map[a_idx] if a_idx is not None else None
        orig_s = sub_idx_map[s_idx] if s_idx is not None else None
        alignment.append((orig_a, orig_s, kind))

    # 5. Compute per-word delays with the selected timing correction.
    log.info("Applying measured pipeline delay correction: +%.3fs", pipeline_offset)

    # First pass: compute all raw delays to establish the expected delay range
    preliminary_delays = []
    for asr_idx, sub_idx, kind in alignment:
        if kind in ("match", "substitution") and asr_idx is not None and sub_idx is not None:
            raw = sub_words[sub_idx].audio_time - asr_words[asr_idx].start + pipeline_offset
            preliminary_delays.append(raw)

    # VOD auto-correction: if the anchor is systematically off (e.g. pre-roll
    # ads captured in audio.wav before the actual programme content begins),
    # detect the offset from the median preliminary delay, correct
    # audio_media_start, reload subtitle words, and re-align.
    if (
        (session_dir / "subtitles.vtt").exists()
        and len(preliminary_delays) >= 20
        and abs(statistics.median(preliminary_delays)) > 30.0
    ):
        correction = statistics.median(preliminary_delays)
        log.warning(
            "VOD anchor off by ~%.0fs (pre-roll ads?). Auto-correcting and re-aligning.",
            correction,
        )
        audio_media_start += correction
        sub_words = load_subtitle_words(session_dir, audio_media_start)
        if sub_words:
            sub_norm = [_normalise(w.word) for w in sub_words]
            sub_valid = [(i, n) for i, n in enumerate(sub_norm) if n]
            sub_filtered = [n for _, n in sub_valid]
            sub_idx_map = [i for i, _ in sub_valid]
            log.info(
                "Re-aligning %d ASR with %d subtitle words after anchor correction...",
                len(asr_filtered), len(sub_filtered),
            )
            raw_alignment = align_words(asr_filtered, sub_filtered)
            alignment = []
            for a_idx, s_idx, kind in raw_alignment:
                orig_a = asr_idx_map[a_idx] if a_idx is not None else None
                orig_s = sub_idx_map[s_idx] if s_idx is not None else None
                alignment.append((orig_a, orig_s, kind))
            preliminary_delays = []
            for a_i, s_i, kind in alignment:
                if kind in ("match", "substitution") and a_i is not None and s_i is not None:
                    raw = sub_words[s_i].audio_time - asr_words[a_i].start + pipeline_offset
                    preliminary_delays.append(raw)

    # Robust estimate of expected delay using median and IQR
    if preliminary_delays:
        sorted_pd = sorted(preliminary_delays)
        median_est = statistics.median(sorted_pd)
        q1 = sorted_pd[len(sorted_pd) // 4]
        q3 = sorted_pd[3 * len(sorted_pd) // 4]
        iqr = q3 - q1
        # Allow a generous band: median ± max(3×IQR, 5s), capped at 90s.
        # For well-aligned sessions IQR is small (1-5s) so this doesn't bite.
        # For VOD sessions with pre-roll ad content in audio, many NW pairs are
        # spuriously matched with delays of hundreds of seconds — the cap stops
        # those from inflating sigma and causing downstream overflow.
        band = min(max(iqr * 3, 5.0), 90.0)
        delay_lo = median_est - band
        delay_hi = median_est + band
        log.info(
            "Delay filter: median=%.2fs IQR=%.2fs → accepting [%.1f, %.1f]s",
            median_est, iqr, delay_lo, delay_hi,
        )
    else:
        delay_lo, delay_hi = -float("inf"), float("inf")

    # Second pass: build aligned words, rejecting temporal outliers
    raw_word_delays = []
    word_delays = []
    aligned_words: list[AlignedWord] = []
    cps_pairs: list[tuple[float, float]] = []  # (chars/s, delay_s) for Rs computation
    matches = subs = insertions = deletions = 0
    rejected = 0

    for asr_idx, sub_idx, kind in alignment:
        asr_t = asr_words[asr_idx].start if asr_idx is not None else None
        sub_t = sub_words[sub_idx].audio_time if sub_idx is not None else None
        asr_w = asr_words[asr_idx].word if asr_idx is not None else None
        sub_w = sub_words[sub_idx].word if sub_idx is not None else None
        delay = None

        if kind in ("match", "substitution") and asr_t is not None and sub_t is not None:
            raw_delay = sub_t - asr_t
            delay = raw_delay + pipeline_offset

            # Reject temporally implausible matches — these are NW
            # misalignments where words from different passages got paired
            if delay < delay_lo or delay > delay_hi:
                rejected += 1
                # Demote to insertion + deletion (unmatched on both sides)
                aligned_words.append(AlignedWord(
                    asr_time=asr_t, sub_time=None,
                    asr_word=asr_w, sub_word=None,
                    delay=None, kind="insertion",
                ))
                aligned_words.append(AlignedWord(
                    asr_time=None, sub_time=sub_t,
                    asr_word=None, sub_word=sub_w,
                    delay=None, kind="deletion",
                ))
                # Adjust counts: undo the match/sub, add ins+del
                insertions += 1
                deletions += 1
                continue

            raw_word_delays.append(raw_delay)
            word_delays.append(delay)

            # Collect (cps, delay) pairs for speech-rate robustness (Rs)
            if asr_idx is not None:
                asr_w_obj = asr_words[asr_idx]
                duration_s = asr_w_obj.end - asr_w_obj.start
                if duration_s >= 0.05 and asr_w_obj.word:
                    cps_pairs.append((len(asr_w_obj.word) / duration_s, delay))

        if kind == "match":
            matches += 1
        elif kind == "substitution":
            subs += 1
        elif kind == "insertion":
            insertions += 1
        elif kind == "deletion":
            deletions += 1

        aligned_words.append(AlignedWord(
            asr_time=asr_t, sub_time=sub_t,
            asr_word=asr_w, sub_word=sub_w,
            delay=delay, kind=kind,
        ))

    if rejected:
        log.info("Rejected %d temporally implausible alignments", rejected)

    delay_events = _build_delay_events(aligned_words)
    if delay_events:
        metric_raw_delays = [event.raw_delay for event in delay_events]
        metric_delays = [event.delay for event in delay_events]
        log.info(
            "Collapsed %d aligned word delays into %d subtitle delay events",
            len(word_delays),
            len(delay_events),
        )
    else:
        metric_raw_delays = raw_word_delays
        metric_delays = word_delays
        log.warning("No subtitle delay events could be built; using per-word delays")

    # 6. WER
    n_ref = len(asr_filtered)
    wer = (subs + insertions + deletions) / n_ref if n_ref > 0 else 0.0
    coverage = matches / n_ref if n_ref > 0 else 0.0

    # 7. Delay stats
    median_raw = statistics.median(metric_raw_delays) if metric_raw_delays else 0.0
    mean_delay = statistics.mean(metric_delays) if metric_delays else None
    median_delay = statistics.median(metric_delays) if metric_delays else None
    delay_std = statistics.stdev(metric_delays) if len(metric_delays) >= 2 else None
    p95_delay = sorted(metric_delays)[int(len(metric_delays) * 0.95)] if metric_delays else None

    # 8. SubLQ from Beyond Latency v2 framework
    sublq = None
    if len(metric_delays) >= 3:
        cps_vals = [c for c, _ in cps_pairs] if len(cps_pairs) >= 2 else None
        lat_per_cps = [d for _, d in cps_pairs] if len(cps_pairs) >= 2 else None
        sublq = metrics.compute_all(metric_delays, cps_values=cps_vals, latency_per_cps_s=lat_per_cps)

    report = EvalReport(
        wer=wer,
        num_asr_words=len(asr_filtered),
        num_sub_words=len(sub_filtered),
        matches=matches,
        substitutions=subs,
        insertions=insertions,
        deletions=deletions,
        coverage=coverage,
        num_delay_events=len(delay_events) if delay_events else len(metric_delays),
        pipeline_offset=pipeline_offset,
        median_raw_delay=median_raw,
        mean_delay=mean_delay,
        median_delay=median_delay,
        p95_delay=p95_delay,
        delay_std=delay_std,
        sublq=sublq,
        delays=metric_delays,
        delay_events=delay_events,
        aligned_words=aligned_words,
    )

    return report


def save_alignment_csv(report: EvalReport, session_dir: Path) -> Path:
    """Save per-word alignment data as CSV for plotting."""
    import csv

    path = session_dir / "alignment.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "asr_time", "sub_time", "asr_word", "sub_word", "delay", "kind",
        ])
        for w in report.aligned_words:
            writer.writerow([
                f"{w.asr_time:.3f}" if w.asr_time is not None else "",
                f"{w.sub_time:.3f}" if w.sub_time is not None else "",
                w.asr_word or "",
                w.sub_word or "",
                f"{w.delay:.3f}" if w.delay is not None else "",
                w.kind,
            ])
    return path


def save_delay_events_csv(report: EvalReport, session_dir: Path) -> Path:
    """Save collapsed subtitle delay events as CSV."""
    import csv

    path = session_dir / "delay_events.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "asr_time", "sub_time", "raw_delay", "delay", "matched_words", "exact_matches",
        ])
        for event in report.delay_events:
            writer.writerow([
                f"{event.asr_time:.3f}",
                f"{event.sub_time:.3f}",
                f"{event.raw_delay:.3f}",
                f"{event.delay:.3f}",
                event.matched_words,
                event.exact_matches,
            ])
    return path


def save_report(report: EvalReport, session_dir: Path) -> Path:
    """Save evaluation report as JSON."""
    out = {
        "wer": round(report.wer, 4),
        "num_asr_words": report.num_asr_words,
        "num_sub_words": report.num_sub_words,
        "matches": report.matches,
        "substitutions": report.substitutions,
        "insertions": report.insertions,
        "deletions": report.deletions,
        "coverage": round(report.coverage, 4),
        "num_delay_events": report.num_delay_events,
        "delay_measurement": "subtitle_event_latest_word",
        "pipeline_offset_s": round(report.pipeline_offset, 3),
        "median_raw_delay_s": round(report.median_raw_delay, 3),
        "mean_delay_s": round(report.mean_delay, 3) if report.mean_delay is not None else None,
        "median_delay_s": round(report.median_delay, 3) if report.median_delay is not None else None,
        "p95_delay_s": round(report.p95_delay, 3) if report.p95_delay is not None else None,
        "delay_std_s": round(report.delay_std, 3) if report.delay_std is not None else None,
    }
    if report.sublq:
        out["sublq"] = report.sublq.to_dict()

    path = session_dir / "evaluation.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    return path


def print_report(report: EvalReport) -> None:
    """Print a human-readable evaluation summary."""
    print("\n" + "=" * 60)
    print("SUBTITLE QUALITY EVALUATION")
    print("=" * 60)
    print(f"  ASR words:        {report.num_asr_words}")
    print(f"  Subtitle words:   {report.num_sub_words}")
    print(f"  Matches:          {report.matches}")
    print(f"  Substitutions:    {report.substitutions}")
    print(f"  Insertions:       {report.insertions}  (in ASR, not in subs)")
    print(f"  Deletions:        {report.deletions}  (in subs, not in ASR)")
    print(f"  Delay events:     {report.num_delay_events}  (subtitle display events)")
    print()
    print(f"  WER:              {report.wer:.1%}")
    print(f"  Coverage:         {report.coverage:.1%}")
    print()
    print(f"  Raw median delay: {report.median_raw_delay:.2f}s  (before pipeline correction)")
    if report.pipeline_offset != 0.0:
        print(f"  Pipeline offset:  {report.pipeline_offset:.2f}s  (subtracted)")
    if report.mean_delay is not None:
        print("  Subtitle delay (+ve = subtitle lags speech):")
        print("    Basis:          One sample per subtitle reveal, using the least-lagging aligned word")
        print(f"    Mean:           {report.mean_delay:.2f}s")
        print(f"    Median:         {report.median_delay:.2f}s")
        print(f"    Std:            {report.delay_std:.2f}s")
        print(f"    P95:            {report.p95_delay:.2f}s")
    if report.sublq:
        r = report.sublq
        print()
        print("  SubLQ metrics:")
        print(f"    mu_l  (mean):       {r.mu_l:.1f} ms")
        print(f"    sigma (std):        {r.sigma_l:.1f} ms")
        print(f"    j_l   (jitter):     {r.j_l:.1f} ms")
        print(f"    CV:                 {r.cv:.4f}")
        print(f"    L95:                {r.l95:.1f} ms")
        print(f"    Rs (robustness):    {r.rs:.4f} ms·s/char")
        print()
        print(f"    SubLQ score:        {r.composite_100:.1f} / 100  (grade {r.grade})")
        if r.gate_triggered:
            print(f"    Floor gate:         capped at {r.gate_cap}")
    print("=" * 60)


# --- CLI ---

def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Evaluate subtitle quality against ASR",
    )
    parser.add_argument("session_dir", type=Path, help="Path to session directory")
    parser.add_argument("--model", default="base", help="Whisper model size (default: base)")
    parser.add_argument(
        "--engine", default="whisperx",
        choices=["whisperx", "faster-whisper"],
        help="ASR engine (default: whisperx).",
    )
    parser.add_argument(
        "--offset", type=float, default=None,
        help="Manual pipeline offset override (seconds). If omitted, uses the "
             "measured audio pipeline delay from session.json.",
    )
    parser.add_argument(
        "--rerun-asr", action="store_true",
        help="Force re-run ASR even if a cached transcript exists.",
    )
    args = parser.parse_args()

    report = evaluate(
        args.session_dir,
        model_size=args.model,
        pipeline_offset=args.offset,
        engine=args.engine,
        rerun_asr=args.rerun_asr,
    )
    print_report(report)

    path = save_report(report, args.session_dir)
    align_path = save_alignment_csv(report, args.session_dir)
    events_path = save_delay_events_csv(report, args.session_dir)
    print(f"\nReport saved: {path}")
    print(f"Alignment CSV: {align_path}")
    print(f"Delay events CSV: {events_path}")

    try:
        from src.plot import plot_latency_timeseries
        ts_path = plot_latency_timeseries(args.session_dir, show=False, save=True)
        if ts_path:
            print(f"Timeseries plot: {ts_path}")
    except Exception as exc:
        log.debug("Could not generate timeseries plot: %s", exc)


if __name__ == "__main__":
    main()
