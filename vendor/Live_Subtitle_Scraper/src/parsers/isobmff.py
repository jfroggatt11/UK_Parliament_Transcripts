"""Minimal ISOBMFF (MP4) box parser for extracting tfdt (track fragment decode time).

The tfdt box contains the base decode time for a media segment, which
tells us exactly where in the media timeline a video segment belongs.
Comparing this against subtitle cue timestamps gives us the true
subtitle-to-video offset.
"""

import logging
import struct

log = logging.getLogger(__name__)


def extract_tfdt(data: bytes) -> tuple[int, int] | None:
    """Extract (base_decode_time, timescale) from an m4s segment.

    Scans for the tfdt box inside a moof (movie fragment) box.
    Returns the raw base_decode_time and looks for timescale in
    the mdhd box of an init segment, or returns None if not found.

    For live segments without an init segment, returns (base_decode_time, 0)
    and the caller must supply the timescale from the manifest or config.
    """
    bdt = _find_tfdt_base_decode_time(data)
    if bdt is None:
        return None
    timescale = _find_mdhd_timescale(data)
    return bdt, timescale or 0


def _find_tfdt_base_decode_time(data: bytes) -> int | None:
    """Find the tfdt box and extract baseMediaDecodeTime."""
    # tfdt box type
    target = b"tfdt"
    offset = 0
    while offset < len(data) - 8:
        # Search for the box type directly (faster than parsing every box)
        idx = data.find(target, offset)
        if idx == -1:
            return None

        # The box size is in the 4 bytes before the box type
        if idx < 4:
            offset = idx + 4
            continue

        box_size = struct.unpack(">I", data[idx - 4 : idx])[0]
        # Version byte follows the box header (type)
        version = data[idx + 4]

        if version == 1:
            # 64-bit baseMediaDecodeTime
            if idx + 16 <= len(data):
                bdt = struct.unpack(">Q", data[idx + 8 : idx + 16])[0]
                log.debug("tfdt v1: bdt=%d (raw bytes: %s)", bdt, data[idx+8:idx+16].hex())
                return bdt
        else:
            # 32-bit baseMediaDecodeTime
            if idx + 12 <= len(data):
                bdt = struct.unpack(">I", data[idx + 8 : idx + 12])[0]
                log.debug("tfdt v0: bdt=%d (raw bytes: %s)", bdt, data[idx+8:idx+12].hex())
                return bdt

        offset = idx + 4
    return None


def _find_mdhd_timescale(data: bytes) -> int | None:
    """Find the mdhd box and extract timescale (only present in init segments)."""
    target = b"mdhd"
    idx = data.find(target)
    if idx == -1 or idx < 4:
        return None

    version = data[idx + 4]
    if version == 1:
        # Skip: version(1) + flags(3) + creation_time(8) + modification_time(8)
        ts_offset = idx + 4 + 1 + 3 + 8 + 8
    else:
        # Skip: version(1) + flags(3) + creation_time(4) + modification_time(4)
        ts_offset = idx + 4 + 1 + 3 + 4 + 4

    if ts_offset + 4 <= len(data):
        return struct.unpack(">I", data[ts_offset : ts_offset + 4])[0]
    return None
