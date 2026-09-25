"""Parser for WebVTT subtitle files (Channel 4).

Channel 4 delivers subtitles as plain WebVTT files with relative timestamps.
Each cue's absolute wall-clock time is:

    segment_anchor + cue_relative_seconds

where segment_anchor = availabilityStartTime + (segment_t / timescale).

Speaker identity is encoded via CSS colour classes (e.g. <c.yellow>, <c.cyan>).
"""

import re
from dataclasses import dataclass

from src.parsers.ttml import Cue

# WebVTT timestamp: HH:MM:SS.mmm or MM:SS.mmm
_VTT_TS_RE = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")

# VTT tag pattern for stripping <c.yellow>, </c>, etc.
_VTT_TAG_RE = re.compile(r"<[^>]+>")


def vtt_timestamp_to_seconds(ts: str) -> float:
    """Convert a WebVTT timestamp like '00:01:23.456' to seconds."""
    m = _VTT_TS_RE.match(ts.strip())
    if not m:
        raise ValueError(f"Cannot parse VTT timestamp: {ts!r}")
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    millis = int(m.group(4))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def parse_webvtt(
    text: str,
    segment_anchor: float,
) -> list[Cue]:
    """Parse a WebVTT file and return Cue objects with absolute Unix timestamps.

    Args:
        text: Raw WebVTT file content.
        segment_anchor: Absolute Unix time for the segment start
                        (availabilityStartTime + segment_t / timescale).

    Returns:
        List of Cue objects with absolute begin/end timestamps.
    """
    cues: list[Cue] = []
    lines = text.strip().splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Look for a cue timing line: "00:00:01.000 --> 00:00:03.500 ..."
        if "-->" in line:
            parts = line.split("-->")
            if len(parts) >= 2:
                begin_rel = vtt_timestamp_to_seconds(parts[0].strip())
                # End timestamp may have positioning metadata after it
                end_part = parts[1].strip().split()[0]
                end_rel = vtt_timestamp_to_seconds(end_part)

                # Collect cue text lines
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip():
                    text_lines.append(lines[i].strip())
                    i += 1

                cue_text = " ".join(text_lines)
                # Strip VTT tags like <c.yellow>, </c>
                cue_text = _VTT_TAG_RE.sub("", cue_text).strip()

                if cue_text:
                    cues.append(Cue(
                        begin_unix=segment_anchor + begin_rel,
                        end_unix=segment_anchor + end_rel,
                        text=cue_text,
                    ))
        i += 1

    return cues
