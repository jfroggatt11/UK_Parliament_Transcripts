"""Abstract base class for live subtitle scrapers."""

import asyncio
import logging
import signal
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from src.clock import get_akamai_time
from src.config import SessionConfig
from src.parsers.isobmff import extract_tfdt
from src.parsers.ttml import Cue
from src.storage import SessionLogger

log = logging.getLogger(__name__)


class SubtitleAccessError(RuntimeError):
    """The subtitle endpoint repeatedly refused access from this session."""


@dataclass(frozen=True, slots=True)
class LatencyRecord:
    """A single latency measurement tied to a subtitle cue."""
    fetch_time: float
    segment_id: str
    cue_begin: float
    cue_end: float | None
    cue_text: str
    video_media_time: float | None  # video decode time at segment start
    subtitle_video_offset: float  # cue_begin - video_media_time (positive = subtitle lags video)
    cdn_latency: float  # fetch_time - video_media_time (how stale CDN content is)
    edge_latency: float | None  # (video_edge - subtitle_edge) * segment_duration — production delay


class BaseScraper(ABC):
    """Base class that all broadcaster scrapers inherit from.

    Subclasses implement segment URL construction and parsing.
    The base class handles the polling loop, latency computation,
    logging, and graceful shutdown.
    """

    def __init__(self, config: SessionConfig, logger: SessionLogger):
        self.config = config
        self.logger = logger
        self._stop = False
        self._last_segment_id: str | None = None
        self._records: list[LatencyRecord] = []
        self._forbidden_responses = 0
        self.audio_start_unix: float | None = None  # set when audio recording begins

    @property
    @abstractmethod
    def poll_interval(self) -> float:
        """Seconds between polling cycles."""

    @abstractmethod
    def get_segment_url(self, ref_time: float) -> tuple[str, str]:
        """Return (subtitle_url, segment_id) for the segment covering ref_time."""

    def get_video_segment_url(self, ref_time: float) -> str | None:
        """Return video segment URL for the same segment, or None if not available."""
        return None

    @abstractmethod
    def parse_segment(self, data: bytes) -> list[Cue]:
        """Parse raw segment bytes into a list of Cue objects."""

    def subtitle_url_for_segment(self, seg_num: int) -> str:
        """Return subtitle URL for a specific segment number."""
        raise NotImplementedError

    def video_url_for_segment(self, seg_num: int) -> str | None:
        """Return video URL for a specific segment number."""
        return None

    def compute_latency(
        self, cues: list[Cue], fetch_time: float, segment_id: str,
        video_media_time: float | None, edge_latency: float | None,
        segment_wall_clock: float | None = None,
    ) -> list[LatencyRecord]:
        records = []
        for cue in cues:
            if video_media_time is not None:
                sv_offset = cue.begin_unix - video_media_time
                cdn_lat = fetch_time - video_media_time
            else:
                sv_offset = 0.0
                # Use segment wall-clock if available (more accurate than
                # per-cue time, which drifts as cues span a whole segment).
                cdn_lat = fetch_time - (segment_wall_clock if segment_wall_clock is not None else cue.begin_unix)

            records.append(
                LatencyRecord(
                    fetch_time=fetch_time,
                    segment_id=segment_id,
                    cue_begin=cue.begin_unix,
                    cue_end=cue.end_unix,
                    cue_text=cue.text,
                    video_media_time=video_media_time,
                    subtitle_video_offset=sv_offset,
                    cdn_latency=cdn_lat,
                    edge_latency=edge_latency,
                )
            )
        return records

    async def run(self, client: httpx.AsyncClient) -> list[LatencyRecord]:
        """Main polling loop. Runs until duration expires or Ctrl+C."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop)

        end_time = time.monotonic() + self.config.duration_minutes * 60
        log.info(
            "Starting %s scraper | channel=%s | duration=%dm",
            self.config.broadcaster,
            self.config.channel,
            self.config.duration_minutes,
        )

        while not self._stop and time.monotonic() < end_time:
            cycle_start = time.monotonic()
            try:
                await self._poll_cycle(client)
            except SubtitleAccessError:
                # Repeating the same denied request cannot repair access.
                # Propagate to the CLI so it reports the real capture failure.
                raise
            except Exception:
                log.exception("Error in poll cycle")

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0, self.poll_interval - elapsed)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        log.info(
            "Scraper stopped | %d records collected", len(self._records)
        )
        return self._records

    async def _poll_cycle(self, client: httpx.AsyncClient) -> None:
        ref_time = await get_akamai_time(client)
        url, segment_id = self.get_segment_url(ref_time)

        if segment_id == self._last_segment_id:
            return  # already processed
        self._last_segment_id = segment_id

        try:
            resp = await client.get(url, timeout=5.0)
        except httpx.TimeoutException:
            log.warning("Timeout fetching segment %s", segment_id)
            return

        fetch_time = await get_akamai_time(client)

        if resp.status_code == 403:
            self._forbidden_responses += 1
            log.warning(
                "403 Forbidden for segment %s — session may need refresh "
                "(re-open iPlayer and copy a fresh base URL with --base-url)",
                segment_id,
            )
            if self._forbidden_responses >= 3:
                raise SubtitleAccessError(
                    "Subtitle capture stopped after 3 consecutive HTTP 403 responses. "
                    "The server refused access; no transcript can be built from denied requests. "
                    "Possible causes include stale stream settings or restrictions on this "
                    "network/runner. For local capture, run ./scripts/run_local.sh --browser "
                    "and check that BBC Parliament plays with subtitles on this computer."
                )
            return
        self._forbidden_responses = 0
        if resp.status_code == 404:
            log.debug("Segment %s not yet available", segment_id)
            return
        if resp.status_code != 200:
            log.warning("HTTP %d for segment %s", resp.status_code, segment_id)
            return

        cues = self.parse_segment(resp.content)
        if not cues:
            log.debug("No cues in segment %s", segment_id)
            return

        # Fetch video segment at same position for timing reference
        video_media_time = await self._fetch_video_time(client, ref_time)

        # Probe the live edge to measure production delay
        edge_latency = await self._probe_edge_latency(client, int(segment_id))

        self._record_subtitles(segment_id, cues)

        records = self.compute_latency(
            cues, fetch_time, segment_id, video_media_time, edge_latency,
        )
        self._records.extend(records)

        for rec in records:
            self.logger.log_record(rec)

        latest = records[-1]
        edge_str = f"{latest.edge_latency:.2f}s" if latest.edge_latency is not None else "n/a"
        log.info(
            "seg=%s | cues=%d | edge_delay=%s | cdn=%.2fs | text=%s",
            segment_id,
            len(cues),
            edge_str,
            latest.cdn_latency,
            latest.cue_text[:60],
        )

    def _record_subtitles(
        self, segment_id: str, cues: list[Cue],
    ) -> None:
        """Save subtitle text for this segment to disk.

        When audio recording is active, only saves cues that fall after
        the audio recording started — earlier cues would be outside the
        audio timeline and unusable for ASR-based evaluation.
        """
        if self.audio_start_unix is not None:
            cues = [c for c in cues if c.begin_unix >= self.audio_start_unix]
            if not cues:
                return
        cue_lines = [
            f"{cue.begin_unix:.3f}\t{cue.end_unix:.3f}\t{cue.text}"
            if cue.end_unix else f"{cue.begin_unix:.3f}\t\t{cue.text}"
            for cue in cues
        ]
        self.logger.save_subtitle_text(segment_id, cue_lines)

    async def _probe_edge_latency(
        self, client: httpx.AsyncClient, current_seg: int,
    ) -> float | None:
        """Probe the CDN live edge to find the newest available segment for each stream.

        The subtitle stream may lag behind the video stream by one or more
        segments if the respeaker hasn't finished yet.  The gap in segment
        numbers × segment duration = observable production delay.
        """
        # Check if subclass supports segment-number URL construction
        try:
            self.subtitle_url_for_segment(current_seg)
        except NotImplementedError:
            return None

        video_url_fn = self.video_url_for_segment
        sub_url_fn = self.subtitle_url_for_segment

        if video_url_fn(current_seg) is None:
            return None  # need both streams to compare

        # We're currently ~8 segments behind the live edge (31s / 3.84s).
        # Probe forward from current position to find the frontier.
        max_ahead = 10  # don't probe more than 10 segments ahead

        async def _find_edge(url_fn, label: str) -> int:
            """Return the highest segment number that returns 200."""
            best = current_seg  # we know current_seg works
            for offset in range(1, max_ahead + 1):
                seg = current_seg + offset
                url = url_fn(seg)
                if url is None:
                    break
                try:
                    resp = await client.get(
                        url, timeout=2.0, headers={"Range": "bytes=0-15"},
                    )
                    if resp.status_code in (200, 206):
                        best = seg
                    elif resp.status_code == 404:
                        break  # past the edge
                    else:
                        break
                except httpx.TimeoutException:
                    break
            return best

        video_edge, sub_edge = await asyncio.gather(
            _find_edge(video_url_fn, "video"),
            _find_edge(sub_url_fn, "subtitle"),
        )

        gap = video_edge - sub_edge  # positive = subtitle lags video
        latency = gap * self.poll_interval  # convert segments to seconds

        log.info(
            "Edge probe: video_edge=+%d sub_edge=+%d gap=%d (%.2fs)",
            video_edge - current_seg,
            sub_edge - current_seg,
            gap,
            latency,
        )
        return latency

    _detected_video_timescale: int = 0

    async def _fetch_video_time(
        self, client: httpx.AsyncClient, ref_time: float
    ) -> float | None:
        """Fetch the video segment at the same position and extract its media time."""
        video_url = self.get_video_segment_url(ref_time)
        if not video_url:
            return None

        try:
            # Range request — we only need the first ~200 bytes for tfdt
            resp = await client.get(
                video_url,
                timeout=3.0,
                headers={"Range": "bytes=0-511"},
            )
            if resp.status_code not in (200, 206):
                return None

            result = extract_tfdt(resp.content)
            if result is None:
                return None

            base_decode_time, timescale = result

            # Use mdhd timescale if available, otherwise auto-detect
            if timescale == 0:
                timescale = self._detected_video_timescale
            if timescale == 0:
                # Derive timescale from current wall-clock time:
                # bdt / timescale ≈ ref_time (tfdt encodes absolute Unix time)
                if ref_time > 0:
                    timescale = round(base_decode_time / ref_time)
                    self._detected_video_timescale = timescale
                    log.info("Auto-detected video timescale: %d", timescale)
            if timescale == 0:
                return None

            # tfdt encodes absolute Unix time directly (no offset needed)
            video_unix = base_decode_time / timescale
            log.debug(
                "Video time: bdt=%d timescale=%d -> video_unix=%.3f",
                base_decode_time, timescale, video_unix,
            )
            return video_unix

        except Exception as exc:
            log.debug("Failed to fetch video segment: %s", exc)
            return None

    def _request_stop(self) -> None:
        log.info("Shutdown requested")
        self._stop = True
