"""Channel 4 live subtitle scraper.

Channel 4 uses MPEG-DASH with a dynamic MPD manifest. Unlike BBC and ITV
(which use TTML in ISOBMFF containers), Channel 4 delivers subtitles as
plain WebVTT files with relative timestamps.

Absolute wall-clock time for each cue is:
    availabilityStartTime + (segment_t / timescale) + cue_relative_seconds

Cues are phrase-level (~2-4s per cue). Speaker identity is encoded via
CSS colour classes (<c.yellow>, <c.cyan>, etc.).
"""

import logging

from src.config import (
    C4_CHANNELS,
    C4_POLL_INTERVAL_S,
    C4_TIMESCALE,
    SessionConfig,
)
from src.parsers.mpd import (
    SubtitleSegmentInfo,
    get_mpd_availability_start,
    get_mpd_location,
    parse_mpd_subtitle_segments,
)
from src.parsers.ttml import Cue
from src.parsers.webvtt import parse_webvtt
from src.scrapers.base import BaseScraper
from src.storage import SessionLogger

log = logging.getLogger(__name__)


class Channel4Scraper(BaseScraper):
    """Scraper for Channel 4 live subtitle segments.

    Requires an MPD URL to be provided (via --mpd-url or --auto).
    The MPD is polled each cycle to discover new subtitle segments.
    """

    def __init__(self, config: SessionConfig, logger: SessionLogger):
        if config.channel not in C4_CHANNELS:
            raise ValueError(
                f"Unknown Channel 4 channel {config.channel!r}. "
                f"Valid: {', '.join(C4_CHANNELS)}"
            )
        if not config.c4_mpd_url:
            raise ValueError(
                "Channel 4 scraper requires --mpd-url. Open Channel 4 in a "
                "browser, enable subtitles, and copy the .mpd URL from the Network tab."
            )
        super().__init__(config, logger)
        self._known_segments: set[str] = set()
        self._latest_segments: list[SubtitleSegmentInfo] = []
        self._availability_start: float | None = None
        self._vod_subtitle_done: bool = False

    @property
    def poll_interval(self) -> float:
        return C4_POLL_INTERVAL_S

    def get_segment_url(self, ref_time: float) -> tuple[str, str]:
        """Return the newest subtitle segment URL from the last manifest poll."""
        if not self._latest_segments:
            return self.config.c4_mpd_url, "__none__"

        for seg in reversed(self._latest_segments):
            if seg.segment_id not in self._known_segments:
                return seg.url, seg.segment_id

        latest = self._latest_segments[-1]
        return latest.url, latest.segment_id

    def parse_segment(self, data: bytes) -> list[Cue]:
        # Not used directly — C4 uses parse_webvtt with segment anchor
        return []

    async def _poll_cycle(self, client) -> None:
        """Override the base poll cycle to add manifest polling.

        Flow: fetch MPD → parse segments → fetch each new VTT file.
        For VOD streams, downloads the sidecar subtitle file directly instead.
        """
        from src.clock import get_akamai_time

        # VOD path: download sidecar subtitle file once and save to disk
        if self.config.c4_subtitle_url and not self._vod_subtitle_done:
            self._vod_subtitle_done = True
            try:
                resp = await client.get(self.config.c4_subtitle_url, timeout=30.0)
                if resp.status_code == 200:
                    out_path = self.logger.output_dir / "subtitles.vtt"
                    out_path.write_bytes(resp.content)
                    cues = parse_webvtt(resp.text, segment_anchor=0.0)
                    log.info(
                        "VOD subtitle sidecar downloaded: %d cues → %s",
                        len(cues), out_path,
                    )
                else:
                    log.warning("Failed to download subtitle sidecar: HTTP %d", resp.status_code)
            except Exception as exc:
                log.warning("Error downloading subtitle sidecar: %s", exc)
            return

        # 1. Fetch and parse the manifest
        try:
            resp = await client.get(self.config.c4_mpd_url, timeout=8.0)
            if resp.status_code != 200:
                log.warning("MPD fetch returned HTTP %d", resp.status_code)
                return
        except Exception as exc:
            log.warning("Failed to fetch MPD: %s", exc)
            return

        # Extract availabilityStartTime on first successful fetch
        if self._availability_start is None:
            self._availability_start = get_mpd_availability_start(resp.content)
            if self._availability_start:
                log.info(
                    "Channel 4 availabilityStartTime: %.3f",
                    self._availability_start,
                )
                self.logger.save_availability_start(self._availability_start)
            else:
                log.warning("No availabilityStartTime in MPD — timestamps will be relative only")

        # Update MPD URL if <Location> provides a refreshed one
        location = get_mpd_location(resp.content)
        if location:
            self.config.c4_mpd_url = location

        segments = parse_mpd_subtitle_segments(resp.content, self.config.c4_mpd_url)
        if not segments:
            log.debug("No subtitle segments found in MPD")
            return

        self._latest_segments = segments

        # 2. Process each new segment
        new_segments = [
            s for s in segments if s.segment_id not in self._known_segments
        ]
        if not new_segments:
            return

        for seg_info in new_segments:
            self._known_segments.add(seg_info.segment_id)
            await self._fetch_and_process(client, seg_info)

    async def _fetch_and_process(
        self, client, seg_info: SubtitleSegmentInfo,
    ) -> None:
        """Fetch a single VTT subtitle file and process its cues."""
        from src.clock import get_akamai_time

        try:
            resp = await client.get(seg_info.url, timeout=5.0)
        except Exception as exc:
            log.warning("Timeout fetching segment %s: %s", seg_info.segment_id, exc)
            return

        fetch_time = await get_akamai_time(client)

        if resp.status_code == 403:
            log.warning("403 Forbidden for segment %s", seg_info.segment_id)
            return
        if resp.status_code == 404:
            log.debug("Segment %s not yet available", seg_info.segment_id)
            return
        if resp.status_code != 200:
            log.warning("HTTP %d for segment %s", resp.status_code, seg_info.segment_id)
            return

        # Compute segment anchor: availabilityStartTime + segment_t / timescale
        if self._availability_start is not None:
            segment_anchor = self._availability_start + seg_info.segment_t / seg_info.timescale
        else:
            # Fallback: use segment wall_clock (segment_t / timescale)
            segment_anchor = seg_info.wall_clock

        # Parse VTT — it's plain text, not a binary container
        vtt_text = resp.text
        cues = parse_webvtt(vtt_text, segment_anchor)
        if not cues:
            log.debug("No cues in segment %s", seg_info.segment_id)
            return

        # Save subtitle text to disk if recording enabled
        if self.config.record_av:
            self._record_subtitles(seg_info.segment_id, cues)

        records = self.compute_latency(
            cues, fetch_time, seg_info.segment_id,
            video_media_time=None,
            edge_latency=None,
            segment_wall_clock=segment_anchor,
        )
        self._records.extend(records)

        for rec in records:
            self.logger.log_record(rec)

        latest = records[-1]
        log.info(
            "seg=%s | cues=%d | cdn=%.2fs | text=%s",
            seg_info.segment_id,
            len(cues),
            latest.cdn_latency,
            latest.cue_text[:60],
        )
