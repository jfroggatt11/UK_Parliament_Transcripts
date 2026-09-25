"""YouTube live stream caption scraper (with optional audio recording).

YouTube delivers auto-generated captions for live streams via a signed HLS
playlist containing segmented WebVTT files.  The playlist is a DVR-type
playlist with a single #EXT-X-PROGRAM-DATE-TIME at the top anchoring the
first segment; subsequent segments are each YOUTUBE_SEGMENT_DURATION_S later.

Caption capture approach:
  1. Use yt-dlp (Python API) to resolve the signed caption playlist URL and
     parse its expiry time (the 'expire' token in the URL).
  2. On each poll cycle, fetch the live HLS playlist, find any segments with
     a sequence number greater than the last processed one, download their
     WebVTT content, and convert relative cue times to absolute Unix time
     using the per-segment wall-clock anchor derived from the PDT header.
  3. Re-resolve the playlist URL via yt-dlp before it expires so the scraper
     can run indefinitely.

Audio recording (--record-av):
  When enabled, the lowest-quality muxed HLS stream (typically 240p, ~200kbps)
  is polled in parallel with captions.  YouTube live delivers MPEG-TS segments
  — each is self-contained, so they are saved to disk and concatenated at
  session end.  ffmpeg then decodes the audio track (no video) to a 16kHz
  mono WAV at output/youtube/SESSION/audio.wav.

  Timing anchor:
    audio_pdt_unix  = #EXT-X-PROGRAM-DATE-TIME of the first audio segment
                      downloaded.  This is the absolute wall-clock time of
                      the first sample in the WAV.
    audio_start_unix = time.time() at which we fetched that segment.
    stream_latency  = audio_start_unix - audio_pdt_unix  (≈ CDN lag, ~7-8s)

  evaluate.py's load_audio_media_start() sees stream_latency ≥ 3 s and
  resolves:  anchor = audio_start_unix - stream_latency = audio_pdt_unix,
  which is the same PDT time-base used for subtitle cue_begin_unix values.
  No calibration beep, no cross-correlation, no MSE timestamp offsets needed.

On startup the scraper jumps to the live edge (last segment in the playlist)
to avoid replaying historical captions from the DVR window.

CDN latency is measured as:  fetch_time - segment_pdt_unix
(i.e. how long after the segment became available we captured it).
"""

import asyncio
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.config import (
    YOUTUBE_POLL_INTERVAL_S,
    YOUTUBE_SEGMENT_DURATION_S,
    YOUTUBE_URL_REFRESH_MARGIN_S,
    SessionConfig,
)
from src.parsers.webvtt import parse_webvtt
from src.parsers.ttml import Cue
from src.scrapers.base import BaseScraper, LatencyRecord
from src.storage import SessionLogger

log = logging.getLogger(__name__)

# Matches the 'expire/UNIX_TS/' token in YouTube signed URLs
_EXPIRE_RE = re.compile(r"/expire/(\d+)/")
# Matches 'sq/DIGITS' sequence number in segment URLs
_SQ_RE = re.compile(r"/sq/(\d+)/")


def _parse_playlist(
    text: str,
) -> tuple[int | None, float | None, list[tuple[int, float, str]]]:
    """Parse a YouTube live HLS playlist (captions or video/audio).

    Returns:
        (base_seq, anchor_pdt_unix, [(seq, pdt_unix, url), ...])
        base_seq is the value of #EXT-X-MEDIA-SEQUENCE.
        anchor_pdt_unix is the PDT of whichever segment the last
        #EXT-X-PROGRAM-DATE-TIME tag was associated with.

    HLS requires that #EXT-X-PROGRAM-DATE-TIME applies to the NEXT media
    segment in the playlist.  YouTube sometimes places this tag once at the
    very top (anchoring the oldest DVR segment) and sometimes only near the
    live edge.  We handle both by tracking a *pending* PDT and anchoring it
    to the first URL that follows it rather than assuming it always refers to
    base_seq.  This prevents the base_pdt + (seq − base_seq) × 5 formula
    from producing future timestamps when the PDT tag appears mid-playlist.
    """
    base_seq: int | None = None
    seq_offset = 0

    # PDT anchor: reset each time a new #EXT-X-PROGRAM-DATE-TIME is seen
    pending_pdt: float | None = None   # PDT from the most recent tag, not yet assigned
    anchor_pdt: float | None = None    # PDT of the anchor segment
    anchor_seq: int | None = None      # seq number of the anchor segment

    segments: list[tuple[int, float, str]] = []

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            base_seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            dt_str = line.split(":", 1)[1]
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            pending_pdt = dt.timestamp()
        elif line.startswith("https://") and base_seq is not None:
            m = _SQ_RE.search(line)
            seq = int(m.group(1)) if m else base_seq + seq_offset

            # A pending PDT tag anchors to this segment
            if pending_pdt is not None:
                anchor_pdt = pending_pdt
                anchor_seq = seq
                pending_pdt = None

            if anchor_pdt is not None and anchor_seq is not None:
                pdt = anchor_pdt + (seq - anchor_seq) * YOUTUBE_SEGMENT_DURATION_S
                segments.append((seq, pdt, line))

            seq_offset += 1

    return base_seq, anchor_pdt, segments


class YouTubeScraper(BaseScraper):
    """Scraper for YouTube live stream auto-generated captions via HLS.

    Optionally records a concurrent audio stream (MPEG-TS segments) when
    config.record_av is True, saving a WAV at session end for ASR evaluation.
    """

    def __init__(self, config: SessionConfig, logger: SessionLogger):
        if not config.youtube_url:
            raise ValueError("SessionConfig.youtube_url must be set for YouTubeScraper")
        super().__init__(config, logger)

        # Caption stream state
        self._playlist_url: str | None = None
        self._playlist_expires_at: float = 0.0

        # Audio stream state (only used when config.record_av is True)
        self._audio_playlist_url: str | None = None
        self._audio_expires_at: float = 0.0
        self._last_audio_seq: int | None = None
        self._audio_pdt_unix: float | None = None   # PDT of first audio segment
        self._audio_start_unix: float | None = None  # wall-clock when first segment fetched
        self._audio_dir: Path | None = None

    # ------------------------------------------------------------------
    # BaseScraper abstract interface — not used; we override _poll_cycle
    # ------------------------------------------------------------------

    @property
    def poll_interval(self) -> float:
        return YOUTUBE_POLL_INTERVAL_S

    def get_segment_url(self, ref_time: float) -> tuple[str, str]:
        raise NotImplementedError("YouTubeScraper uses HLS playlist polling")

    def parse_segment(self, data: bytes) -> list[Cue]:
        raise NotImplementedError("YouTubeScraper uses HLS playlist polling")

    # ------------------------------------------------------------------
    # Playlist URL management
    # ------------------------------------------------------------------

    def _resolve_playlist_url(self) -> None:
        """Call yt-dlp (blocking) to get fresh signed playlist URLs."""
        import yt_dlp

        ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(self.config.youtube_url, download=False)

        # Caption playlist
        caps = info.get("automatic_captions", {})
        en_caps = caps.get("en", [])
        if not en_caps:
            raise RuntimeError(
                f"No English auto-captions found for {self.config.youtube_url}. "
                "The stream may not have live captions enabled."
            )
        caption_url = en_caps[0]["url"]
        self._playlist_url = caption_url
        m = _EXPIRE_RE.search(caption_url)
        self._playlist_expires_at = float(m.group(1)) if m else time.time() + 1800

        # Audio playlist — lowest quality muxed HLS (smallest download overhead)
        if self.config.record_av:
            fmts = info.get("formats", [])
            # Muxed HLS formats have both vcodec and acodec set
            muxed = [
                f for f in fmts
                if f.get("protocol") in ("m3u8", "m3u8_native")
                and f.get("vcodec", "none") not in ("none", "")
                and f.get("acodec", "none") not in ("none", "")
            ]
            if muxed:
                # Pick lowest bitrate to minimise bandwidth
                best = min(muxed, key=lambda f: f.get("tbr") or f.get("abr") or 9999)
                self._audio_playlist_url = best["url"]
                m2 = _EXPIRE_RE.search(best["url"])
                self._audio_expires_at = float(m2.group(1)) if m2 else time.time() + 1800
                log.info(
                    "Audio stream: format_id=%s acodec=%s (tbr=%s)",
                    best.get("format_id"), best.get("acodec"), best.get("tbr"),
                )
            else:
                log.warning("No muxed HLS format found — audio recording unavailable")

        log.info(
            "Resolved YouTube playlist URLs (expires in %.0fs)",
            self._playlist_expires_at - time.time(),
        )

    def _url_needs_refresh(self) -> bool:
        return (
            self._playlist_url is None
            or time.time() >= self._playlist_expires_at - YOUTUBE_URL_REFRESH_MARGIN_S
        )

    def _audio_url_needs_refresh(self) -> bool:
        return (
            self._audio_playlist_url is None
            or time.time() >= self._audio_expires_at - YOUTUBE_URL_REFRESH_MARGIN_S
        )

    # ------------------------------------------------------------------
    # Run loop — overridden to add audio finalisation
    # ------------------------------------------------------------------

    async def run(self, client: httpx.AsyncClient) -> list[LatencyRecord]:
        """Run caption + audio capture, then build WAV on completion."""
        if self.config.record_av:
            self._audio_dir = self.logger.output_dir / "audio_segments"
            self._audio_dir.mkdir(exist_ok=True)

        records = await super().run(client)

        if self.config.record_av and self._audio_pdt_unix is not None:
            await asyncio.to_thread(self._finalise_audio)

        return records

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def _poll_cycle(self, client: httpx.AsyncClient) -> None:
        # Refresh signed URLs if stale or about to expire
        if self._url_needs_refresh():
            log.info("Refreshing YouTube playlist URLs via yt-dlp…")
            await asyncio.to_thread(self._resolve_playlist_url)
            # Reset sequence trackers so the next poll re-anchors to the new
            # playlist's live edge.  Without this, the PDT formula
            # base_pdt + (seq - base_seq) * 5  produces timestamps ~2500 s in
            # the future whenever the refreshed playlist has a base_seq near 0
            # while _last_segment_id still holds the pre-refresh sequence number.
            self._last_segment_id = None
            self._last_audio_seq = None
            log.info("Sequence trackers reset — will re-anchor to new live edge")

        # Run caption and audio polling concurrently
        tasks = [self._poll_captions(client)]
        if self.config.record_av and self._audio_playlist_url:
            tasks.append(self._poll_audio(client))

        await asyncio.gather(*tasks)

    async def _poll_captions(self, client: httpx.AsyncClient) -> None:
        """Fetch the caption HLS playlist and process any new segments."""
        try:
            resp = await client.get(self._playlist_url, timeout=10.0)
        except httpx.TimeoutException:
            log.warning("Timeout fetching YouTube caption playlist")
            return

        if resp.status_code == 403:
            log.warning("403 on caption playlist — forcing URL refresh next cycle")
            self._playlist_url = None
            return
        if resp.status_code != 200:
            log.warning("HTTP %d fetching YouTube caption playlist", resp.status_code)
            return

        fetch_time = time.time()
        _base_seq, _base_pdt, segments = _parse_playlist(resp.text)
        if not segments:
            log.debug("No segments in caption playlist")
            return

        # On first call, jump to the live edge
        if self._last_segment_id is None:
            last_seq = segments[-1][0]
            self._last_segment_id = str(last_seq - 1)
            log.info(
                "Caption live edge: seq=%d (%d segments in DVR window)",
                last_seq, len(segments),
            )

        last_processed = int(self._last_segment_id)
        new_segs = [(seq, pdt, url) for seq, pdt, url in segments if seq > last_processed]

        for seq, pdt_unix, seg_url in new_segs:
            await self._process_caption_segment(client, seq, pdt_unix, seg_url, fetch_time)

    async def _poll_audio(self, client: httpx.AsyncClient) -> None:
        """Fetch the audio HLS playlist and save any new MPEG-TS segments."""
        if self._audio_url_needs_refresh():
            # URL refresh is handled in _poll_cycle before this is called;
            # if still None here, skip this cycle.
            if self._audio_playlist_url is None:
                return

        try:
            resp = await client.get(self._audio_playlist_url, timeout=10.0)
        except httpx.TimeoutException:
            log.warning("Timeout fetching YouTube audio playlist")
            return

        if resp.status_code == 403:
            log.warning("403 on audio playlist — forcing URL refresh next cycle")
            self._audio_playlist_url = None
            return
        if resp.status_code != 200:
            log.warning("HTTP %d fetching YouTube audio playlist", resp.status_code)
            return

        _base_seq, _base_pdt, segments = _parse_playlist(resp.text)
        if not segments:
            return

        # Jump to live edge on first audio poll
        if self._last_audio_seq is None:
            last_seq = segments[-1][0]
            self._last_audio_seq = last_seq - 1
            log.info("Audio live edge: seq=%d", last_seq)

        new_segs = [
            (seq, pdt, url) for seq, pdt, url in segments
            if seq > self._last_audio_seq
        ]

        for seq, pdt_unix, seg_url in new_segs:
            await self._save_audio_segment(client, seq, pdt_unix, seg_url)

    async def _save_audio_segment(
        self,
        client: httpx.AsyncClient,
        seq: int,
        pdt_unix: float,
        seg_url: str,
    ) -> None:
        """Download one MPEG-TS audio segment and save it to disk."""
        try:
            resp = await client.get(seg_url, timeout=10.0)
        except httpx.TimeoutException:
            log.warning("Timeout fetching audio segment sq/%d", seq)
            return

        fetch_time = time.time()

        if resp.status_code != 200:
            log.warning("HTTP %d for audio segment sq/%d", resp.status_code, seq)
            return

        # Record timing anchor from the first segment we download
        if self._audio_pdt_unix is None:
            self._audio_pdt_unix = pdt_unix
            self._audio_start_unix = fetch_time
            log.info(
                "Audio recording started: pdt=%s stream_latency=%.2fs",
                datetime.fromtimestamp(pdt_unix, tz=timezone.utc).strftime("%H:%M:%S"),
                fetch_time - pdt_unix,
            )

        seg_path = self._audio_dir / f"{seq:09d}.ts"
        seg_path.write_bytes(resp.content)
        self._last_audio_seq = seq
        log.debug("Audio sq=%d saved (%.1f KB)", seq, len(resp.content) / 1024)

    # ------------------------------------------------------------------
    # Caption segment processing
    # ------------------------------------------------------------------

    async def _process_caption_segment(
        self,
        client: httpx.AsyncClient,
        seq: int,
        pdt_unix: float,
        seg_url: str,
        playlist_fetch_time: float,
    ) -> None:
        """Download and process a single WebVTT caption segment."""
        try:
            resp = await client.get(seg_url, timeout=8.0)
        except httpx.TimeoutException:
            log.warning("Timeout fetching caption segment sq/%d", seq)
            return

        seg_fetch_time = time.time()

        if resp.status_code != 200:
            log.warning("HTTP %d fetching caption segment sq/%d", resp.status_code, seq)
            return

        cues = parse_webvtt(resp.text, segment_anchor=pdt_unix)
        if not cues:
            log.debug("No cues in segment sq/%d", seq)
            self._last_segment_id = str(seq)
            return

        self._last_segment_id = str(seq)
        segment_id = str(seq)

        self._record_subtitles(segment_id, cues)

        records = self.compute_latency(
            cues,
            fetch_time=seg_fetch_time,
            segment_id=segment_id,
            video_media_time=None,
            edge_latency=None,
            segment_wall_clock=pdt_unix,
        )
        self._records.extend(records)
        for rec in records:
            self.logger.log_record(rec)

        latest = records[-1]
        log.info(
            "sq=%s | cues=%d | cdn=%.2fs | pdt=%s | text=%s",
            seq,
            len(cues),
            latest.cdn_latency,
            datetime.fromtimestamp(pdt_unix, tz=timezone.utc).strftime("%H:%M:%S"),
            latest.cue_text[:60],
        )

    # ------------------------------------------------------------------
    # Audio finalisation
    # ------------------------------------------------------------------

    def _finalise_audio(self) -> None:
        """Concatenate MPEG-TS segments and decode to 16kHz mono WAV."""
        if self._audio_dir is None or self._audio_pdt_unix is None:
            return

        ts_files = sorted(self._audio_dir.glob("*.ts"))
        if not ts_files:
            log.warning("No audio segments to finalise")
            return

        log.info("Finalising audio: concatenating %d MPEG-TS segments…", len(ts_files))

        # Concatenate all TS segments (MPEG-TS is byte-concatenatable)
        concat_path = self._audio_dir / "concat.ts"
        with open(concat_path, "wb") as out:
            for ts_path in ts_files:
                out.write(ts_path.read_bytes())

        wav_path = self.logger.output_dir / "audio.wav"
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(concat_path),
                    "-vn",           # drop video track
                    "-ar", "16000",  # 16 kHz for Whisper
                    "-ac", "1",      # mono
                    str(wav_path),
                ],
                capture_output=True,
                timeout=120,
            )
        except FileNotFoundError:
            log.error("ffmpeg not found — cannot produce audio.wav")
            return
        except subprocess.TimeoutExpired:
            log.error("ffmpeg timed out decoding audio")
            return

        if result.returncode != 0:
            log.error(
                "ffmpeg failed (rc=%d): %s",
                result.returncode,
                result.stderr[-300:].decode(errors="replace"),
            )
            return

        concat_path.unlink(missing_ok=True)

        duration_s = len(ts_files) * YOUTUBE_SEGMENT_DURATION_S
        log.info(
            "Audio WAV saved: %s (%.0fs of content)",
            wav_path, duration_s,
        )

        # Write timing anchor to session.json so evaluate.py can align ASR
        stream_latency = self._audio_start_unix - self._audio_pdt_unix
        self.logger.save_audio_start(
            unix_ts=self._audio_start_unix,
            stream_latency=stream_latency,
            stream_latency_method="pdt_direct",
        )
        log.info(
            "Audio anchor: pdt=%.3f start=%.3f stream_latency=%.2fs",
            self._audio_pdt_unix, self._audio_start_unix, stream_latency,
        )
