"""BBC iPlayer live subtitle scraper.

BBC uses a static DASH manifest with number-based segment templates.
Segment numbers are derived directly from wall clock time:

    segment_number = floor((now_unix - 25) / 3.84)

Subtitles are EBU-TT-D (TTML) wrapped in ISOBMFF .m4s containers,
with word-level cues whose timestamps encode Unix wall-clock time
directly in the hours field.
"""

import math

from src.config import (
    BBC_AVAILABILITY_START_OFFSET_S,
    BBC_CDN_BASE,
    BBC_CHANNELS,
    BBC_SEGMENT_DURATION_S,
    BBC_SUBTITLE_REPR,
    SessionConfig,
)
from src.parsers.ttml import Cue, parse_ttml_segment
from src.scrapers.base import BaseScraper
from src.storage import SessionLogger


class BBCScraper(BaseScraper):
    """Scraper for BBC iPlayer live subtitle segments."""

    def __init__(self, config: SessionConfig, logger: SessionLogger):
        if config.channel not in BBC_CHANNELS:
            raise ValueError(
                f"Unknown BBC channel {config.channel!r}. "
                f"Valid: {', '.join(BBC_CHANNELS)}"
            )
        super().__init__(config, logger)

    @property
    def poll_interval(self) -> float:
        return BBC_SEGMENT_DURATION_S

    def _segment_number(self, ref_time: float) -> int:
        return math.floor(
            (ref_time - BBC_AVAILABILITY_START_OFFSET_S) / BBC_SEGMENT_DURATION_S
        )

    def get_segment_url(self, ref_time: float) -> tuple[str, str]:
        seg_num = self._segment_number(ref_time)
        return self._subtitle_url(seg_num), str(seg_num)

    def get_video_segment_url(self, ref_time: float) -> str | None:
        if not self.config.bbc_video_repr:
            return None
        seg_num = self._segment_number(ref_time)
        return self._video_url(seg_num)

    def subtitle_url_for_segment(self, seg_num: int) -> str:
        return self._subtitle_url(seg_num)

    def video_url_for_segment(self, seg_num: int) -> str | None:
        if not self.config.bbc_video_repr:
            return None
        return self._video_url(seg_num)

    def _subtitle_url(self, seg_num: int) -> str:
        base = self.config.bbc_base_url
        x = self.config.bbc_x_param
        channel = self.config.channel
        return (
            f"{base}/x={x}/"
            f"i=urn:bbc:pips:service:{channel}/"
            f"t=3840/{BBC_SUBTITLE_REPR}/{seg_num}.m4s"
        )

    def _video_url(self, seg_num: int) -> str:
        base = self.config.bbc_video_base_url or self.config.bbc_base_url
        x = self.config.bbc_x_param
        channel = self.config.channel
        return (
            f"{base}/x={x}/"
            f"i=urn:bbc:pips:service:{channel}/"
            f"{self.config.bbc_video_repr}/{seg_num}.m4s"
        )

    def parse_segment(self, data: bytes) -> list[Cue]:
        return parse_ttml_segment(data)
