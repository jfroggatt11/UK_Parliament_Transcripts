"""Configuration constants for the Live Subtitle Scraper."""

from dataclasses import dataclass, field
from pathlib import Path

# --- Akamai UTC reference ---
AKAMAI_TIME_URL = "https://time.akamai.com/?iso"
AKAMAI_TIMEOUT_S = 2.0
AKAMAI_CACHE_TTL_S = 0.5

# --- Output defaults ---
DEFAULT_OUTPUT_DIR = Path("output")

# --- BBC iPlayer ---
BBC_CDN_BASE = "https://vs-cmaf-push-uk.live.fastly.md.bbci.co.uk"
BBC_X_PARAM = 4
BBC_SEGMENT_DURATION_S = 3.84
BBC_AVAILABILITY_START_OFFSET_S = 25
BBC_SUBTITLE_REPR = "s=caption1/b=64000"
BBC_TIMESCALE = 3840  # t= path param value

BBC_CHANNELS = {
    "bbc_parliament": "BBC Parliament",
    "bbc_one_hd": "BBC One HD",
    "bbc_one_london": "BBC One London",
    "bbc_one_scotland_hd": "BBC One Scotland",
    "bbc_one_wales_hd": "BBC One Wales",
    "bbc_one_northern_ireland_hd": "BBC One NI",
    "bbc_two_hd": "BBC Two HD",
    "bbc_two_northern_ireland_hd": "BBC Two NI",
    "bbc_two_wales_digital": "BBC Two Wales",
    "bbc_news24": "BBC News",
    "bbc_news_channel_hd": "BBC News HD",
    "bbc_three_hd": "BBC Three HD",
}

# --- ITV (ITVX) ---
ITV_SEGMENT_DURATION_S = 5.76
ITV_POLL_INTERVAL_S = 6
ITV_TIMESCALE = 1000

ITV_CHANNELS = {
    "itv1": "ITV1",
    "itv2": "ITV2",
    "itv3": "ITV3",
    "itv4": "ITV4",
    "itvbe": "ITVBe",
    "citv": "CITV",
    "fast18": "ITVX Exclusive",
}

# --- Channel 4 ---
C4_SEGMENT_DURATION_S = 4.0
C4_POLL_INTERVAL_S = 4
C4_TIMESCALE = 10_000_000

C4_CHANNELS = {
    "channel4": "Channel 4",
    "e4": "E4",
    "more4": "More4",
    "film4": "Film4",
    "4seven": "4seven",
}

# --- Amazon Prime Video ---
AMAZON_SEGMENT_DURATION_S = 2.0
AMAZON_POLL_INTERVAL_S = 5
AMAZON_TIMESCALE = 90_000

AMAZON_CHANNELS = {
    "amazon_cnn": "CNN Headlines",
    "amazon_premier_league": "Premier League",
    "amazon_nfl": "NFL",
    "amazon_champions_league": "Champions League",
}

# --- YouTube ---
YOUTUBE_SEGMENT_DURATION_S = 5.0
YOUTUBE_POLL_INTERVAL_S = 5
# Signed playlist URLs expire; refresh yt-dlp info this many seconds before expiry
YOUTUBE_URL_REFRESH_MARGIN_S = 120


@dataclass
class SessionConfig:
    """Runtime configuration for a scraping session."""
    broadcaster: str
    channel: str
    duration_minutes: int = 60
    output_dir: Path = DEFAULT_OUTPUT_DIR
    # BBC-specific overrides
    bbc_base_url: str = BBC_CDN_BASE
    bbc_x_param: int = BBC_X_PARAM
    bbc_video_repr: str | None = None  # e.g. "t=3840/v=pv10/b=1604032"
    bbc_video_base_url: str | None = None  # may differ from subtitle CDN
    record_av: bool = False  # save subtitle text per segment to disk
    # ITV-specific
    itv_mpd_url: str | None = None  # dynamic MPD URL from browser
    # Channel 4-specific
    c4_mpd_url: str | None = None   # dynamic MPD URL from browser
    c4_subtitle_url: str | None = None  # sidecar subtitle URL (VOD only)
    # Amazon Prime Video-specific
    amazon_mpd_url: str | None = None  # dynamic MPD URL from browser
    # YouTube-specific
    youtube_url: str | None = None  # full YouTube watch URL (live stream)
