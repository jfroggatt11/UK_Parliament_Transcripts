"""CSV and JSON logging for subtitle latency capture sessions."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from src.config import SessionConfig

if TYPE_CHECKING:
    from src.scrapers.base import LatencyRecord

log = logging.getLogger(__name__)

CSV_HEADERS = [
    "timestamp",
    "channel",
    "broadcaster",
    "segment_id",
    "cue_begin",
    "cue_end",
    "cue_text",
    "fetch_time",
    "video_media_time",
    "subtitle_video_offset",
    "cdn_latency",
    "edge_latency",
]


class SessionLogger:
    """Writes per-cue latency records to CSV and session metadata to JSON."""

    def __init__(self, config: SessionConfig):
        self.config = config
        self._session_start = datetime.now(timezone.utc)
        self._session_id = self._session_start.strftime("%Y%m%d_%H%M%S")

        self._output_dir = config.output_dir / config.broadcaster / self._session_id
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._csv_path = self._output_dir / "latency.csv"
        self._meta_path = self._output_dir / "session.json"

        # Write CSV header
        with open(self._csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

        # Write initial session metadata
        self._write_metadata()

        # Subtitle text directory — always created
        self._subs_dir = self._output_dir / "subs"
        self._subs_dir.mkdir(exist_ok=True)

        log.info("Session output: %s", self._output_dir)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def subs_dir(self) -> Path | None:
        return self._subs_dir

    def save_subtitle_text(self, segment_id: str, cue_lines: list[str]) -> Path | None:
        """Save subtitle text for a segment (one line per cue)."""
        if not self._subs_dir:
            return None
        path = self._subs_dir / f"{segment_id}.txt"
        path.write_text("\n".join(cue_lines) + "\n", encoding="utf-8")
        return path

    def log_record(self, rec: LatencyRecord) -> None:
        with open(self._csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.fromtimestamp(rec.fetch_time, tz=timezone.utc).isoformat(),
                self.config.channel,
                self.config.broadcaster,
                rec.segment_id,
                f"{rec.cue_begin:.3f}",
                f"{rec.cue_end:.3f}" if rec.cue_end else "",
                rec.cue_text,
                f"{rec.fetch_time:.3f}",
                f"{rec.video_media_time:.3f}" if rec.video_media_time else "",
                f"{rec.subtitle_video_offset:.3f}",
                f"{rec.cdn_latency:.3f}",
                f"{rec.edge_latency:.3f}" if rec.edge_latency is not None else "",
            ])

    def save_availability_start(self, unix_ts: float) -> None:
        """Record the MPD availabilityStartTime."""
        self._availability_start_time = unix_ts
        self._write_metadata()

    def save_audio_start(
        self,
        unix_ts: float,
        video_time: float | None = None,
        calibration_beep_offset: float | None = None,
        video_buffered_end: float | None = None,
        stream_latency: float | None = None,
        mse_timestamp_offset: float | None = None,
        video_source_frame: str | None = None,
        video_num_candidates: int | None = None,
        stream_latency_method: str | None = None,
    ) -> None:
        """Record timing info for when audio recording began.

        Args:
            unix_ts: Wall clock Unix timestamp when MediaRecorder started.
            video_time: The video element's currentTime at recording start.
                For DASH live, this is seconds since availabilityStartTime.
            calibration_beep_offset: Wall-clock offset (seconds from recording
                start) when a 6kHz calibration tone was injected into tab audio.
            video_buffered_end: The video element's buffered.end() at recording
                start. Used with DASH segment times to compute MSE timestamp
                offset for Amazon-style streams.
            stream_latency: Seconds between live edge and browser playback,
                computed from video segment tfdt timestamps. Used to correct
                the audio anchor for accurate subtitle delay measurement.
        """
        self._audio_start_unix = unix_ts
        self._video_time_at_start = video_time
        self._calibration_beep_offset = calibration_beep_offset
        self._video_buffered_end = video_buffered_end
        if stream_latency is not None:
            self._stream_latency = stream_latency
        if mse_timestamp_offset is not None:
            self._mse_timestamp_offset = mse_timestamp_offset
        if video_source_frame is not None:
            self._video_source_frame = video_source_frame
        if video_num_candidates is not None:
            self._video_num_candidates = video_num_candidates
        if stream_latency_method is not None:
            self._stream_latency_method = stream_latency_method
        self._write_metadata()

    def update_video_time(self, video_time: float) -> None:
        """Override video_time_at_start (e.g. with DASH presentation time)."""
        self._video_time_at_start = video_time
        self._write_metadata()

    def finalise(self, record_count: int) -> None:
        """Update session metadata with end time and record count."""
        self._write_metadata(
            end_time=datetime.now(timezone.utc).isoformat(),
            record_count=record_count,
        )

    def _write_metadata(
        self,
        end_time: str | None = None,
        record_count: int = 0,
    ) -> None:
        meta = {
            "session_id": self._session_id,
            "broadcaster": self.config.broadcaster,
            "channel": self.config.channel,
            "start_time": self._session_start.isoformat(),
            "audio_start_unix": getattr(self, "_audio_start_unix", None),
            "video_time_at_start": getattr(self, "_video_time_at_start", None),
            "calibration_beep_offset": getattr(self, "_calibration_beep_offset", None),
            "video_buffered_end": getattr(self, "_video_buffered_end", None),
            "stream_latency": getattr(self, "_stream_latency", None),
            "mse_timestamp_offset": getattr(self, "_mse_timestamp_offset", None),
            "video_source_frame": getattr(self, "_video_source_frame", None),
            "video_num_candidates": getattr(self, "_video_num_candidates", None),
            "stream_latency_method": getattr(self, "_stream_latency_method", None),
            "availability_start_time": getattr(self, "_availability_start_time", None),
            "end_time": end_time,
            "duration_minutes": self.config.duration_minutes,
            "record_count": record_count,
            "config": {
                "bbc_base_url": self.config.bbc_base_url,
                "bbc_x_param": self.config.bbc_x_param,
                "bbc_video_repr": self.config.bbc_video_repr,
                "itv_mpd_url": self.config.itv_mpd_url,
                "c4_mpd_url": self.config.c4_mpd_url,
                "record_av": self.config.record_av,
            },
        }
        with open(self._meta_path, "w") as f:
            json.dump(meta, f, indent=2)
