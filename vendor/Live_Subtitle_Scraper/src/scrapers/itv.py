"""ITV (ITVX) live subtitle scraper.

ITV uses MPEG-DASH with a dynamic MPD manifest. Unlike BBC (where segment
numbers are derived from wall-clock time), ITV requires manifest polling
to discover new subtitle segments via the SegmentTimeline.

Subtitles are TTML in ISOBMFF .dash containers — the same parser as BBC.
Cue timestamps use the same large-hours format encoding Unix wall-clock time.
Cues are phrase-level (~2-3s per cue) rather than BBC's word-level.
"""

import logging

from src.config import (
    ITV_CHANNELS,
    ITV_POLL_INTERVAL_S,
    SessionConfig,
)
from src.parsers.mpd import SubtitleSegmentInfo, get_mpd_location, parse_mpd_subtitle_segments
from src.parsers.ttml import Cue, parse_ttml_segment
from src.scrapers.base import BaseScraper
from src.storage import SessionLogger

log = logging.getLogger(__name__)


class ITVScraper(BaseScraper):
    """Scraper for ITV (ITVX) live subtitle segments.

    Requires an MPD URL to be provided (either manually via --mpd-url
    or captured from the browser). The MPD is polled each cycle to
    discover new subtitle segments.
    """

    def __init__(self, config: SessionConfig, logger: SessionLogger):
        if config.channel not in ITV_CHANNELS:
            raise ValueError(
                f"Unknown ITV channel {config.channel!r}. "
                f"Valid: {', '.join(ITV_CHANNELS)}"
            )
        if not config.itv_mpd_url:
            raise ValueError(
                "ITV scraper requires --mpd-url. Open ITVX in a browser, "
                "enable subtitles, and copy the .mpd URL from the Network tab."
            )
        super().__init__(config, logger)
        self._known_segments: set[str] = set()
        self._latest_segments: list[SubtitleSegmentInfo] = []

    @property
    def poll_interval(self) -> float:
        return ITV_POLL_INTERVAL_S

    def get_segment_url(self, ref_time: float) -> tuple[str, str]:
        """Return the newest subtitle segment URL from the last manifest poll.

        For ITV, ref_time is not used for URL construction — the manifest
        provides the segment list. This method returns the newest unseen
        segment, or the latest segment if all have been seen.
        """
        if not self._latest_segments:
            # No manifest parsed yet — return a dummy that will 404
            return self.config.itv_mpd_url, "__none__"

        # Find newest unseen segment
        for seg in reversed(self._latest_segments):
            if seg.segment_id not in self._known_segments:
                return seg.url, seg.segment_id

        # All seen — return latest (will be deduped by base class)
        latest = self._latest_segments[-1]
        return latest.url, latest.segment_id

    def parse_segment(self, data: bytes) -> list[Cue]:
        return parse_ttml_segment(data)

    async def _poll_cycle(self, client) -> None:
        """Override the base poll cycle to add manifest polling.

        Flow: fetch MPD → parse segments → fetch each new subtitle segment.
        """
        from src.clock import get_akamai_time

        # 1. Fetch and parse the manifest
        try:
            resp = await client.get(self.config.itv_mpd_url, timeout=8.0)
            if resp.status_code != 200:
                log.warning("MPD fetch returned HTTP %d", resp.status_code)
                return
        except Exception as exc:
            log.warning("Failed to fetch MPD: %s", exc)
            return

        # Update MPD URL if <Location> provides a refreshed one
        location = get_mpd_location(resp.content)
        if location:
            self.config.itv_mpd_url = location

        segments = parse_mpd_subtitle_segments(resp.content, self.config.itv_mpd_url)
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
        """Fetch a single subtitle segment and process its cues."""
        from src.clock import get_akamai_time

        try:
            resp = await client.get(seg_info.url, timeout=5.0)
        except Exception as exc:
            log.warning("Timeout fetching segment %s: %s", seg_info.segment_id, exc)
            return

        fetch_time = await get_akamai_time(client)

        if resp.status_code == 403:
            log.warning(
                "403 Forbidden for segment %s — JWT may have expired. "
                "Re-open ITVX and copy a fresh MPD URL.",
                seg_info.segment_id,
            )
            return
        if resp.status_code == 404:
            log.debug("Segment %s not yet available", seg_info.segment_id)
            return
        if resp.status_code != 200:
            log.warning("HTTP %d for segment %s", resp.status_code, seg_info.segment_id)
            return

        cues = self.parse_segment(resp.content)
        if not cues:
            log.debug("No cues in segment %s", seg_info.segment_id)
            return

        # Save subtitle text to disk if recording enabled
        if self.config.record_av:
            self._record_subtitles(seg_info.segment_id, cues)

        # ITV doesn't have separate video segments we can easily probe,
        # so we use cue timestamps directly for latency
        records = self.compute_latency(
            cues, fetch_time, seg_info.segment_id,
            video_media_time=None,
            edge_latency=None,
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
