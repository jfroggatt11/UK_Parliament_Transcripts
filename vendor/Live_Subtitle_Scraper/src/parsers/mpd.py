"""MPD (MPEG-DASH manifest) parser for ITV and Channel 4 live streams.

Extracts subtitle segment URLs from dynamic MPD manifests that use
SegmentTimeline with $Time$ templates.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from lxml import etree

log = logging.getLogger(__name__)

_DASH_NS = "urn:mpeg:dash:schema:mpd:2011"


@dataclass(frozen=True, slots=True)
class SubtitleSegmentInfo:
    """Info about the latest subtitle segment from the manifest."""
    url: str
    segment_t: int          # raw timeline t value
    timescale: int          # timescale from the AdaptationSet/SegmentTemplate
    wall_clock: float       # segment_t / timescale = Unix wall-clock time
    segment_id: str         # string ID for dedup (typically the t value)


def parse_mpd_subtitle_segments(
    mpd_bytes: bytes,
    mpd_url: str,
) -> list[SubtitleSegmentInfo]:
    """Parse a dynamic MPD and return subtitle segment info.

    Finds the subtitle AdaptationSet (contentType="text" or mimeType
    containing "mp4"/"ttml"/"vtt"), extracts the SegmentTimeline, and
    builds full segment URLs.

    Supports both $Time$ and $Number$ segment templates.

    Args:
        mpd_bytes: Raw MPD XML content.
        mpd_url: The URL the MPD was fetched from (for resolving relative URLs).

    Returns:
        List of SubtitleSegmentInfo for all segments in the timeline,
        ordered by time (newest last).
    """
    root = etree.fromstring(mpd_bytes)
    ns = {"mpd": _DASH_NS}

    # Collect subtitle segments from ALL Periods. Amazon's MPD contains
    # multiple Periods (content + ad breaks); only some have subtitles.
    # We need segments from every Period that has a subtitle track.
    all_segments: list[SubtitleSegmentInfo] = []

    # Find all subtitle AdaptationSets across all Periods
    subtitle_adapts = _find_all_subtitle_adaptation_sets(root, ns)
    if not subtitle_adapts:
        # Fall back to legacy single-match for ITV/C4 compatibility
        adapt = _find_subtitle_adaptation_set(root, ns)
        if adapt is not None:
            subtitle_adapts = [adapt]

    for adapt in subtitle_adapts:
        seg_template = adapt.find(".//mpd:SegmentTemplate", ns)
        if seg_template is None:
            continue

        timescale = int(seg_template.get("timescale", "1"))
        media_template = seg_template.get("media", "")

        # Get RepresentationID for $RepresentationID$ substitution
        rep_elem = adapt.find("mpd:Representation", ns)
        rep_id = rep_elem.get("id", "") if rep_elem is not None else ""
        media_template = media_template.replace("$RepresentationID$", rep_id)

        # Determine if this is a $Number$ or $Time$ template
        uses_number = "$Number$" in media_template
        start_number = int(seg_template.get("startNumber", "1"))

        # Resolve base URL for segments (relative to this Period's context)
        base_url = _resolve_base_url(root, adapt, mpd_url, ns)

        # Extract timeline entries
        timeline = seg_template.find("mpd:SegmentTimeline", ns)
        if timeline is None:
            continue

        t = 0
        seg_number = start_number
        for s_elem in timeline.findall("mpd:S", ns):
            if s_elem.get("t") is not None:
                t = int(s_elem.get("t"))
            d = int(s_elem.get("d", "0"))
            r = int(s_elem.get("r", "0"))

            for _ in range(r + 1):
                if uses_number:
                    url = media_template.replace("$Number$", str(seg_number))
                else:
                    url = media_template.replace("$Time$", str(t))
                url = _resolve_url(base_url, url)

                wall_clock = t / timescale
                segment_id = str(seg_number) if uses_number else str(t)
                all_segments.append(SubtitleSegmentInfo(
                    url=url,
                    segment_t=t,
                    timescale=timescale,
                    wall_clock=wall_clock,
                    segment_id=segment_id,
                ))
                t += d
                seg_number += 1

    return all_segments


def get_mpd_location(mpd_bytes: bytes) -> str | None:
    """Extract <Location> URL from MPD for subsequent manifest polls."""
    root = etree.fromstring(mpd_bytes)
    loc = root.find(f"{{{_DASH_NS}}}Location")
    if loc is not None and loc.text:
        return loc.text.strip()
    return None


def get_mpd_type(mpd_bytes: bytes) -> str | None:
    """Extract the MPD type attribute (e.g. dynamic or static)."""
    root = etree.fromstring(mpd_bytes)
    mpd_type = root.get("type")
    return mpd_type.lower() if mpd_type else None


def get_mpd_availability_start(mpd_bytes: bytes) -> float | None:
    """Extract availabilityStartTime from MPD as Unix timestamp."""
    root = etree.fromstring(mpd_bytes)
    ast = root.get("availabilityStartTime")
    if not ast:
        return None
    dt = datetime.fromisoformat(ast.replace("Z", "+00:00"))
    return dt.timestamp()


def get_mpd_utc_timing(mpd_bytes: bytes) -> float | None:
    """Extract inline UTC timing value from MPD.

    Amazon embeds the current wall-clock time as a SupplementalProperty
    or UTCTiming element rather than referencing an external NTP endpoint.
    Returns a Unix timestamp if found, otherwise None.
    """
    root = etree.fromstring(mpd_bytes)

    # Check UTCTiming elements (schemeIdUri="urn:mpeg:dash:utc:direct:2014")
    for elem in root.iter(f"{{{_DASH_NS}}}UTCTiming"):
        scheme = elem.get("schemeIdUri", "")
        if "direct" in scheme and elem.get("value"):
            try:
                dt = datetime.fromisoformat(elem.get("value").replace("Z", "+00:00"))
                return dt.timestamp()
            except (ValueError, TypeError):
                pass

    # Check SupplementalProperty with UTC scheme
    for elem in root.iter(f"{{{_DASH_NS}}}SupplementalProperty"):
        scheme = elem.get("schemeIdUri", "")
        if "utc" in scheme.lower() and elem.get("value"):
            try:
                dt = datetime.fromisoformat(elem.get("value").replace("Z", "+00:00"))
                return dt.timestamp()
            except (ValueError, TypeError):
                pass

    return None


@dataclass(frozen=True, slots=True)
class AudioSegmentInfo:
    """Info about a DASH audio segment."""
    url: str
    segment_t: int          # raw timeline t value
    timescale: int
    dash_time: float        # segment_t / timescale (seconds)
    segment_id: str
    group_id: str
    init_url: str


def parse_mpd_audio_segments(
    mpd_bytes: bytes,
    mpd_url: str,
) -> list[AudioSegmentInfo]:
    """Parse a DASH MPD and return audio segment info across all periods.

    Amazon MPDs may contain multiple periods and/or change init segments during
    pre-recorded inserts. Each returned segment carries the init URL and a
    stable group_id so callers can keep discontinuous runs separate.
    """
    root = etree.fromstring(mpd_bytes)
    ns = {"mpd": _DASH_NS}

    all_segments: list[AudioSegmentInfo] = []
    group_counter = 0

    for period in root.findall("mpd:Period", ns):
        for adapt in period.findall("mpd:AdaptationSet", ns):
            mime = adapt.get("mimeType", "").lower()
            content_type = adapt.get("contentType", "").lower()
            if "audio" not in mime and "audio" not in content_type:
                continue

            seg_template = adapt.find(".//mpd:SegmentTemplate", ns)
            if seg_template is None:
                continue

            timescale = int(seg_template.get("timescale", "1"))
            media_template = seg_template.get("media", "")
            init_template = seg_template.get("initialization", "")

            rep = adapt.find("mpd:Representation", ns)
            rep_id = rep.get("id", "") if rep is not None else ""
            media_template = media_template.replace("$RepresentationID$", rep_id)
            init_template = init_template.replace("$RepresentationID$", rep_id)

            base_url = _resolve_base_url(root, adapt, mpd_url, ns)
            init_url = _resolve_url(base_url, init_template)
            period_id = period.get("id", f"period{group_counter}")
            adapt_id = adapt.get("id", f"adapt{group_counter}")
            raw_group_id = f"{period_id}_{adapt_id}_{rep_id or 'rep'}"
            group_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_group_id)
            group_counter += 1

            timeline = seg_template.find("mpd:SegmentTimeline", ns)
            if timeline is None:
                continue

            uses_number = "$Number$" in media_template
            start_number = int(seg_template.get("startNumber", "1"))

            t = 0
            seg_number = start_number
            for s_elem in timeline.findall("mpd:S", ns):
                if s_elem.get("t") is not None:
                    t = int(s_elem.get("t"))
                d = int(s_elem.get("d", "0"))
                r = int(s_elem.get("r", "0"))

                for _ in range(r + 1):
                    if uses_number:
                        url = media_template.replace("$Number$", str(seg_number))
                    else:
                        url = media_template.replace("$Time$", str(t))
                    url = _resolve_url(base_url, url)

                    all_segments.append(AudioSegmentInfo(
                        url=url,
                        segment_t=t,
                        timescale=timescale,
                        dash_time=t / timescale,
                        segment_id=str(seg_number) if uses_number else str(t),
                        group_id=group_id,
                        init_url=init_url,
                    ))
                    t += d
                    seg_number += 1

    all_segments.sort(key=lambda s: (s.dash_time, s.group_id, s.segment_id))

    return all_segments


def _is_subtitle_adaptation_set(adapt, ns: dict) -> bool:
    """Check whether an AdaptationSet contains subtitle/text content."""
    content_type = adapt.get("contentType", "").lower()
    mime_type = adapt.get("mimeType", "").lower()
    codecs = adapt.get("codecs", "").lower()

    if content_type == "text":
        return True
    if "ttml" in mime_type or "stpp" in mime_type:
        return True
    if "vtt" in mime_type or "webvtt" in mime_type:
        return True
    if "stpp" in codecs:
        return True

    for rep in adapt.findall("mpd:Representation", ns):
        rep_mime = rep.get("mimeType", "").lower()
        rep_codecs = rep.get("codecs", "").lower()
        if "ttml" in rep_mime or "stpp" in rep_mime or "vtt" in rep_mime:
            return True
        if "stpp" in rep_codecs:
            return True

    return False


def _find_all_subtitle_adaptation_sets(root, ns: dict) -> list:
    """Find all subtitle AdaptationSets across all Periods in the MPD.

    Amazon's MPD contains multiple Periods (content + ad breaks).
    Only some Periods have subtitle tracks. This returns subtitle
    AdaptationSets from every Period that has one.
    """
    results = []
    for period in root.findall("mpd:Period", ns):
        for adapt in period.findall("mpd:AdaptationSet", ns):
            if _is_subtitle_adaptation_set(adapt, ns):
                results.append(adapt)
    return results


def _find_subtitle_adaptation_set(root, ns: dict):
    """Find the subtitle/text AdaptationSet in the MPD (legacy single-match)."""
    for adapt in root.iter(f"{{{_DASH_NS}}}AdaptationSet"):
        if _is_subtitle_adaptation_set(adapt, ns):
            return adapt
    return None


def _resolve_base_url(root, adapt, mpd_url: str, ns: dict) -> str:
    """Resolve the base URL for segment requests.

    Per DASH spec, BaseURL elements at each level are concatenated:
    MPD BaseURL + Period BaseURL + AdaptationSet BaseURL.
    A relative BaseURL extends its parent; an absolute one replaces it.

    If the final result is still relative, it is resolved against the
    MPD URL (needed for Amazon's deeply relative ../../../ BaseURLs).
    """
    # Strip query params from MPD URL for base resolution
    mpd_base = mpd_url.split("?")[0]

    # Collect BaseURL at each level: MPD → Period → AdaptationSet
    parts: list[str] = []
    for elem in (root, adapt.getparent(), adapt):
        if elem is None:
            continue
        base = elem.find(f"{{{_DASH_NS}}}BaseURL")
        if base is not None and base.text:
            url = base.text.strip()
            if url.startswith("http://") or url.startswith("https://"):
                parts = [url]  # absolute URL resets the chain
            else:
                parts.append(url)

    if parts:
        combined = "".join(parts)
        # If the combined result is still relative, resolve against the MPD URL
        if not combined.startswith("http://") and not combined.startswith("https://"):
            combined = urljoin(mpd_base, combined)
        return combined

    # Fall back to MPD URL directory
    return mpd_base.rsplit("/", 1)[0] + "/"


def _resolve_url(base: str, path: str) -> str:
    """Resolve a potentially relative URL against a base.

    Uses urllib.parse.urljoin to correctly handle ../ traversal in
    relative paths (e.g. Amazon's deeply relative segment URLs).
    """
    if path.startswith("http://") or path.startswith("https://"):
        return path
    # Ensure base ends with / so urljoin treats it as a directory
    if not base.endswith("/"):
        base = base.rsplit("/", 1)[0] + "/"
    return urljoin(base, path)
