"""Amazon Prime Video live subtitle scraper.

Amazon uses MPEG-DASH with a dynamic MPD manifest. Subtitles are TTML (stpp)
in MP4 containers — the same format as BBC/ITV, but with a critical difference:
TTML timestamps are media time relative to availabilityStartTime, not direct
Unix wall-clock time.

To convert: cue_wall_clock = availabilityStartTime_unix + (hours*3600 + mins*60 + secs)

The manifest embeds the current wall-clock time inline as a SupplementalProperty
rather than referencing an external NTP endpoint.

Cues are word-level (17-433ms) in US CEA-608 style (ALL CAPS, >> speaker change).
Segment duration is 2s with a 90,000 timescale (broadcast standard).
"""

import json
import logging

from src.config import (
    AMAZON_CHANNELS,
    AMAZON_POLL_INTERVAL_S,
    SessionConfig,
)
from src.parsers.mpd import (
    SubtitleSegmentInfo,
    get_mpd_availability_start,
    get_mpd_location,
    get_mpd_type,
    parse_mpd_audio_segments,
    parse_mpd_subtitle_segments,
)
from src.parsers.ttml import Cue, parse_ttml_segment
from src.scrapers.base import BaseScraper
from src.storage import SessionLogger

log = logging.getLogger(__name__)


class AmazonScraper(BaseScraper):
    """Scraper for Amazon Prime Video live subtitle segments.

    Requires an MPD URL to be provided (either manually via --mpd-url
    or captured from the browser). The MPD is polled each cycle to
    discover new subtitle segments.
    """

    def __init__(self, config: SessionConfig, logger: SessionLogger):
        if config.channel not in AMAZON_CHANNELS:
            raise ValueError(
                f"Unknown Amazon channel {config.channel!r}. "
                f"Valid: {', '.join(AMAZON_CHANNELS)}"
            )
        if not config.amazon_mpd_url:
            raise ValueError(
                "Amazon scraper requires --mpd-url. Open Amazon Prime Video "
                "in a browser, start a live channel, and copy the .mpd URL "
                "from the Network tab."
            )
        super().__init__(config, logger)
        self._known_segments: set[str] = set()
        self._known_audio_segments: set[str] = set()
        self._latest_segments: list[SubtitleSegmentInfo] = []
        self._availability_start: float | None = None
        self._audio_dir = self.logger.output_dir / "dash_audio"
        self._audio_dir.mkdir(exist_ok=True)
        self._audio_first_dash_time: float | None = None
        self._audio_last_dash_time: float | None = None
        self._audio_saved_count = 0
        self._audio_init_saved: set[str] = set()
        self._audio_encrypted = False

    @property
    def poll_interval(self) -> float:
        return AMAZON_POLL_INTERVAL_S

    def get_segment_url(self, ref_time: float) -> tuple[str, str]:
        """Return the newest subtitle segment URL from the last manifest poll."""
        if not self._latest_segments:
            return self.config.amazon_mpd_url, "__none__"

        for seg in reversed(self._latest_segments):
            if seg.segment_id not in self._known_segments:
                return seg.url, seg.segment_id

        latest = self._latest_segments[-1]
        return latest.url, latest.segment_id

    def parse_segment(self, data: bytes) -> list[Cue]:
        return parse_ttml_segment(data, epoch_offset=self._availability_start or 0.0)

    def _write_audio_meta(self) -> None:
        """Persist the DASH audio timeline anchor for later evaluation."""
        if (
            self._availability_start is None
            or self._audio_first_dash_time is None
            or self._audio_last_dash_time is None
            or self._audio_saved_count == 0
        ):
            return
        meta_path = self._audio_dir / "meta.json"
        meta_path.write_text(json.dumps({
            "ast": self._availability_start,
            "first_dash_time": self._audio_first_dash_time,
            "last_dash_time": self._audio_last_dash_time,
            "num_segments": self._audio_saved_count,
            "encrypted": self._audio_encrypted,
        }, indent=2))

    async def _sync_audio_reference(self, client, mpd_bytes: bytes) -> None:
        """Save DASH audio init/media segments so evaluation can use stream-time audio."""
        audio_segments = parse_mpd_audio_segments(mpd_bytes, self.config.amazon_mpd_url)
        if not audio_segments:
            log.debug("No audio segments found in MPD")
            return
        self._audio_encrypted = any(
            "cenc_" in seg.init_url.lower() or "cenc_" in seg.url.lower()
            for seg in audio_segments
        )

        init_by_group = {seg.group_id: seg.init_url for seg in audio_segments}
        for group_id, init_url in init_by_group.items():
            if group_id in self._audio_init_saved:
                continue
            init_path = self._audio_dir / f"init_{group_id}.mp4"
            if init_path.exists():
                self._audio_init_saved.add(group_id)
                continue
            try:
                init_resp = await client.get(init_url, timeout=8.0)
            except Exception as exc:
                log.debug("Failed to fetch DASH audio init for %s: %s", group_id, exc)
                continue
            if init_resp.status_code not in (200, 206):
                log.warning("Audio init fetch HTTP %d for %s", init_resp.status_code, group_id)
                continue
            init_path.write_bytes(init_resp.content)
            # Preserve the legacy filename for single-init helpers.
            legacy_init = self._audio_dir / "init.mp4"
            if not legacy_init.exists():
                legacy_init.write_bytes(init_resp.content)
            self._audio_init_saved.add(group_id)
            log.debug("Saved DASH audio init %s (%d bytes)", group_id, len(init_resp.content))

        if not self._audio_init_saved:
            return

        new_audio_segments = [
            seg for seg in audio_segments
            if f"{seg.group_id}:{seg.segment_id}" not in self._known_audio_segments
        ]
        if not new_audio_segments:
            return

        # Mirror subtitle capture: on startup the MPD exposes the full time-shift
        # buffer. Keep only the newest few audio segments so evaluation covers the
        # live window we actually measured instead of stale buffered content.
        if len(self._known_audio_segments) == 0 and len(new_audio_segments) > 5:
            skipped = new_audio_segments[:-5]
            for seg in skipped:
                self._known_audio_segments.add(f"{seg.group_id}:{seg.segment_id}")
            new_audio_segments = new_audio_segments[-5:]
            log.debug("First poll: skipping %d old audio segments", len(skipped))

        for seg in new_audio_segments:
            seg_key = f"{seg.group_id}:{seg.segment_id}"
            seg_path = self._audio_dir / f"{seg.dash_time:.3f}_{seg.group_id}_{seg.segment_id}.m4s"
            if seg_path.exists():
                self._known_audio_segments.add(seg_key)
                continue
            try:
                resp = await client.get(seg.url, timeout=5.0)
            except Exception as exc:
                log.debug("Failed to fetch DASH audio segment %s: %s", seg.segment_id, exc)
                continue
            if resp.status_code == 404:
                log.debug("DASH audio segment %s not yet available", seg.segment_id)
                continue
            if resp.status_code not in (200, 206):
                log.debug("DASH audio segment %s HTTP %d", seg.segment_id, resp.status_code)
                continue
            seg_path.write_bytes(resp.content)
            self._known_audio_segments.add(seg_key)
            self._audio_first_dash_time = (
                seg.dash_time if self._audio_first_dash_time is None
                else min(self._audio_first_dash_time, seg.dash_time)
            )
            self._audio_last_dash_time = (
                seg.dash_time if self._audio_last_dash_time is None
                else max(self._audio_last_dash_time, seg.dash_time)
            )
            self._audio_saved_count += 1

        self._write_audio_meta()

    async def _poll_cycle(self, client) -> None:
        """Override the base poll cycle to add manifest polling.

        Flow: fetch MPD → parse segments → fetch each new subtitle segment.
        """
        # 1. Fetch and parse the manifest
        try:
            resp = await client.get(self.config.amazon_mpd_url, timeout=8.0)
            if resp.status_code != 200:
                log.warning("MPD fetch returned HTTP %d", resp.status_code)
                return
        except Exception as exc:
            log.warning("Failed to fetch MPD: %s", exc)
            return

        # Save the first MPD we saw for debugging even if it later turns out
        # to be a preview/replay manifest rather than the live stream.
        if not hasattr(self, "_mpd_saved"):
            debug_path = self.logger.output_dir / "debug_mpd.xml"
            debug_path.write_bytes(resp.content)
            log.debug("Saved MPD to %s", debug_path)
            self._mpd_saved = True

        mpd_type = get_mpd_type(resp.content)
        if mpd_type != "dynamic":
            if getattr(self, "_bad_mpd_reason", None) != f"type={mpd_type}":
                log.warning(
                    "Amazon MPD is %s, not a dynamic live manifest. "
                    "This usually means the captured URL came from preview/replay "
                    "content rather than the live match.",
                    mpd_type or "unknown",
                )
                self._bad_mpd_reason = f"type={mpd_type}"
            return

        # Extract availabilityStartTime on first successful fetch
        if self._availability_start is None:
            self._availability_start = get_mpd_availability_start(resp.content)
            if self._availability_start:
                log.info(
                    "Amazon availabilityStartTime: %.3f",
                    self._availability_start,
                )
                self.logger.save_availability_start(self._availability_start)
            else:
                if getattr(self, "_bad_mpd_reason", None) != "missing_ast":
                    log.warning(
                        "Amazon MPD has no availabilityStartTime. "
                        "This usually means the captured manifest is not the live stream."
                    )
                    self._bad_mpd_reason = "missing_ast"
                return

        # Update MPD URL if <Location> provides a refreshed one
        location = get_mpd_location(resp.content)
        if location:
            self.config.amazon_mpd_url = location

        await self._sync_audio_reference(client, resp.content)

        segments = parse_mpd_subtitle_segments(resp.content, self.config.amazon_mpd_url)
        if not segments:
            log.debug("No subtitle segments found in MPD (check AdaptationSet matching)")
            return
        log.debug("Found %d subtitle segments in MPD (first URL: %s)", len(segments), segments[0].url[:200])

        self._latest_segments = segments

        # Check if video.currentTime is a valid DASH presentation time.
        # Amazon's MSE player may set timestampOffset so that currentTime
        # IS the DASH presentation time (~89M seconds). If so, AST + vct
        # is already correct and we don't need to override.
        # Only override with the live-edge wall_clock when vct is clearly
        # wrong (0, None, or a small local offset like 279s).
        if not hasattr(self, '_dash_time_saved') and self.config.record_av:
            latest = segments[-1]
            vct = getattr(self.logger, '_video_time_at_start', None)
            ast = self._availability_start or 0
            if vct and ast:
                anchor = ast + vct
                session_start = getattr(self.logger, '_session_start', None)
                drift = abs(anchor - (session_start.timestamp() if session_start else 0))
                if drift > 3600:
                    # vct is not on DASH timeline — override with live edge
                    self.logger.update_video_time(latest.wall_clock)
                    log.info(
                        "DASH override: vct=%.3f invalid, using live edge %.3f (AST+wc=%.3f)",
                        vct, latest.wall_clock, ast + latest.wall_clock,
                    )
                else:
                    log.info(
                        "DASH anchor from browser: vct=%.3f (AST+vct=%.3f)",
                        vct, anchor,
                    )
            elif vct is None or vct == 0:
                self.logger.update_video_time(latest.wall_clock)
                log.info(
                    "DASH fallback to live edge: %.3f (AST+wc=%.3f)",
                    latest.wall_clock, ast + latest.wall_clock,
                )
            self._dash_time_saved = True

        # 2. Process each new segment
        new_segments = [
            s for s in segments if s.segment_id not in self._known_segments
        ]
        if not new_segments:
            return

        # On the first poll, the timeline contains the entire timeShift buffer
        # (~150 segments / 300s). Only process the most recent few to avoid
        # fetching stale segments with incorrect latency measurements.
        if len(self._known_segments) == 0 and len(new_segments) > 5:
            skipped = new_segments[:-5]
            for seg in skipped:
                self._known_segments.add(seg.segment_id)
            new_segments = new_segments[-5:]
            log.debug("First poll: skipping %d old buffered segments", len(skipped))

        for seg_info in new_segments:
            self._known_segments.add(seg_info.segment_id)
            await self._fetch_and_process(client, seg_info)

    async def _fetch_and_process(
        self, client, seg_info: SubtitleSegmentInfo,
    ) -> None:
        """Fetch a single subtitle segment and process its cues."""
        try:
            resp = await client.get(seg_info.url, timeout=5.0)
        except Exception as exc:
            log.warning("Timeout fetching segment %s: %s", seg_info.segment_id, exc)
            return

        # Use Akamai time for accurate fetch timestamp
        from src.clock import get_akamai_time
        fetch_time = await get_akamai_time(client)

        if resp.status_code == 403:
            log.warning(
                "403 Forbidden for segment %s — session may have expired. "
                "Re-open Amazon Prime Video and copy a fresh MPD URL.",
                seg_info.segment_id,
            )
            return
        if resp.status_code == 404:
            log.debug("Segment %s not yet available", seg_info.segment_id)
            return
        if resp.status_code != 200:
            log.warning("HTTP %d for segment %s", resp.status_code, seg_info.segment_id)
            return

        # Save first segment for debugging
        if not hasattr(self, '_seg_saved'):
            debug_path = self.logger.output_dir / f"debug_segment_{seg_info.segment_id}.mp4"
            debug_path.write_bytes(resp.content)
            log.debug("Saved segment to %s (%d bytes)", debug_path, len(resp.content))
            self._seg_saved = True

        cues = self.parse_segment(resp.content)
        if not cues:
            log.debug("No cues in segment %s", seg_info.segment_id)
            return

        self._record_subtitles(seg_info.segment_id, cues)

        segment_wall_clock = (self._availability_start or 0) + seg_info.wall_clock
        records = self.compute_latency(
            cues, fetch_time, seg_info.segment_id,
            video_media_time=None,
            edge_latency=None,
            segment_wall_clock=segment_wall_clock,
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
