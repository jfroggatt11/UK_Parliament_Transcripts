"""Parser for EBU-TT-D (TTML) subtitles wrapped in ISOBMFF/m4s containers.

Used by both BBC and ITV scrapers. Handles:
- Extracting XML payload from MP4 box structure
- Parsing TTML cue elements with wall-clock timestamps
- Converting the large-hours format (e.g. 492876:30:35.520) to Unix time
"""

import re
from dataclasses import dataclass
from lxml import etree

# Regex for TTML timestamp: hours:minutes:seconds.fraction
# BBC/ITV use extremely large hour values that encode Unix time directly.
_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

# Common TTML namespaces
_NAMESPACES = {
    "tt": "http://www.w3.org/ns/ttml",
    "ttp": "http://www.w3.org/ns/ttml#parameter",
    "tts": "http://www.w3.org/ns/ttml#styling",
}


@dataclass(frozen=True, slots=True)
class Cue:
    """A single subtitle cue with absolute Unix timestamps."""
    begin_unix: float
    end_unix: float | None
    text: str


def ttml_timestamp_to_unix(ts: str) -> float:
    """Convert a TTML timestamp like '492876:30:35.520' to a Unix timestamp.

    Formula: hours * 3600 + minutes * 60 + seconds
    This works because BBC/ITV encode wall-clock time directly in the hours field.
    """
    m = _TS_RE.match(ts)
    if not m:
        raise ValueError(f"Cannot parse TTML timestamp: {ts!r}")
    hours = int(m.group(1))
    minutes = int(m.group(2))
    seconds = float(m.group(3))
    return hours * 3600 + minutes * 60 + seconds


def extract_xml_from_m4s(data: bytes) -> list[bytes]:
    """Extract TTML XML payload(s) from an ISOBMFF/m4s container.

    Scans for the start of XML documents (<?xml or <tt) within the
    raw bytes, skipping MP4 box headers. Truncates each fragment at
    the closing </tt> tag to avoid trailing binary box data.

    Some containers (e.g. Amazon) may pack multiple TTML documents
    into a single segment — one per mdat box.

    Returns a list of XML byte strings (usually one, but may be more).
    """
    fragments: list[bytes] = []

    search_from = 0
    while True:
        idx = data.find(b"<?xml", search_from)
        if idx == -1:
            break
        # Truncate at closing </tt> tag to avoid trailing binary data
        end_tag = data.find(b"</tt>", idx)
        if end_tag != -1:
            fragments.append(data[idx:end_tag + 5])
        else:
            # No closing tag — try next <?xml boundary
            next_idx = data.find(b"<?xml", idx + 5)
            fragments.append(data[idx:next_idx] if next_idx != -1 else data[idx:])
        search_from = idx + 5

    if not fragments:
        # Fall back to bare <tt namespace root
        idx = data.find(b"<tt")
        if idx == -1:
            raise ValueError("No TTML XML payload found in m4s container")
        end_tag = data.find(b"</tt>", idx)
        fragments.append(data[idx:end_tag + 5] if end_tag != -1 else data[idx:])

    return fragments


def parse_ttml_segment(data: bytes, epoch_offset: float = 0.0) -> list[Cue]:
    """Parse an m4s segment containing EBU-TT-D TTML and return cues.

    Each <p> element with a begin attribute becomes a Cue. Text is
    extracted from all child text nodes, stripped of whitespace.

    Handles containers with multiple TTML documents (e.g. Amazon packs
    multiple documents into a single MP4 segment).

    Args:
        data: Raw segment bytes (ISOBMFF/m4s container with TTML payload).
        epoch_offset: Offset to add to parsed timestamps. For BBC/ITV this
            is 0.0 because their large-hours timestamps are direct Unix time.
            For Amazon, this is the availabilityStartTime as a Unix timestamp,
            since Amazon's TTML timestamps are media time relative to that.
    """
    xml_fragments = extract_xml_from_m4s(data)

    cues: list[Cue] = []
    for xml_bytes in xml_fragments:
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError:
            continue

        # Detect namespace — try tt: prefix first, fall back to default ns
        ns = root.nsmap.get("tt") or root.nsmap.get(None, _NAMESPACES["tt"])

        for p in root.iter(f"{{{ns}}}p"):
            begin_str = p.get("begin")
            if not begin_str:
                continue

            begin_unix = ttml_timestamp_to_unix(begin_str) + epoch_offset

            end_str = p.get("end")
            end_unix = (ttml_timestamp_to_unix(end_str) + epoch_offset) if end_str else None

            # Gather all text content (handles nested <span> elements)
            text = "".join(p.itertext()).strip()
            if text:
                cues.append(Cue(begin_unix=begin_unix, end_unix=end_unix, text=text))

    return cues
