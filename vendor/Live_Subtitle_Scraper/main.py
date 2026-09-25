"""CLI entry point for the Live Subtitle Scraper.

Usage:
    python main.py --channel bbc_news24 --duration 60
    python main.py --channel bbc_one_london --auto --duration 30
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

from src.config import BBC_CHANNELS, ITV_CHANNELS, C4_CHANNELS, AMAZON_CHANNELS, DEFAULT_OUTPUT_DIR, SessionConfig
from src.metrics import compute_all
from src.scrapers.amazon import AmazonScraper
from src.scrapers.bbc import BBCScraper
from src.scrapers.channel4 import Channel4Scraper
from src.scrapers.itv import ITVScraper
from src.scrapers.youtube import YouTubeScraper
from src.storage import SessionLogger

BROADCASTERS = {
    "bbc": {"channels": BBC_CHANNELS, "scraper_cls": BBCScraper},
    "itv": {"channels": ITV_CHANNELS, "scraper_cls": ITVScraper},
    "channel4": {"channels": C4_CHANNELS, "scraper_cls": Channel4Scraper},
    "amazon": {"channels": AMAZON_CHANNELS, "scraper_cls": AmazonScraper},
}


ALL_CHANNELS = {
    channel_id: (broadcaster, display_name)
    for broadcaster, info in BROADCASTERS.items()
    for channel_id, display_name in info["channels"].items()
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest live subtitle segments and measure delivery latency.",
        epilog="Available channels:\n"
        + "\n".join(
            f"  {cid:<20s} {bcast.upper()} — {name}"
            for cid, (bcast, name) in sorted(ALL_CHANNELS.items())
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--list-channels",
        action="store_true",
        help="List all available channels and exit.",
    )
    parser.add_argument(
        "--broadcaster",
        choices=list(BROADCASTERS),
        help="Broadcaster (auto-detected from channel if omitted).",
    )
    parser.add_argument(
        "--channel",
        choices=list(ALL_CHANNELS),
        help="Channel to scrape.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Capture duration in minutes (default: 60).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override CDN base URL (e.g. for BBC session-specific x= param).",
    )
    parser.add_argument(
        "--x-param",
        type=int,
        default=None,
        help="Override BBC x= path parameter (default: 4).",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-capture session by opening iPlayer in Chromium (requires playwright).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (use with --auto).",
    )
    parser.add_argument(
        "--mpd-url",
        default=None,
        help="MPD manifest URL (required for ITV/Channel 4/Amazon). Copy from browser Network tab.",
    )
    parser.add_argument(
        "--live-url",
        default=None,
        help="Override the live stream page URL opened by --auto (currently used for Amazon).",
    )
    parser.add_argument(
        "--record-av",
        action="store_true",
        help="Save audio segments (.m4s) and subtitle text per segment for later analysis.",
    )
    parser.add_argument(
        "--wait-for-enter",
        action="store_true",
        help="(Amazon) Pause before MPD capture and wait for Enter — useful to ensure the correct stream is selected.",
    )
    parser.add_argument(
        "--youtube-url",
        default=None,
        metavar="URL",
        help="YouTube live stream URL (e.g. https://www.youtube.com/watch?v=...). "
             "When provided, --channel is not required.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def print_summary(records: list) -> None:
    """Print final session summary with metrics from the paper framework."""
    if not records:
        print("\nNo records collected.")
        return

    # Per-segment: take first record per segment
    seen_segments = set()
    per_seg_edge = []
    per_seg_cdn = []
    for r in records:
        if r.segment_id not in seen_segments:
            seen_segments.add(r.segment_id)
            if r.edge_latency is not None:
                per_seg_edge.append(r.edge_latency)
            per_seg_cdn.append(r.cdn_latency)

    print("\n" + "=" * 60)
    print("SESSION SUMMARY")
    print("=" * 60)
    print(f"  Segments collected:    {len(per_seg_cdn)}")
    print(f"  Total cues:            {len(records)}")
    print()

    if per_seg_edge:
        edge_metrics = compute_all(per_seg_edge)
        print("  Subtitle production delay (subtitle edge lag vs video edge):")
        print(f"    Mean (μL):           {edge_metrics.mu_l:.2f}s")
        print(f"    Std deviation (σL):  {edge_metrics.sigma_l:.2f}s")
        print(f"    Jitter (JL):         {edge_metrics.j_l:.2f}s")
        print(f"    Coeff of var (CV):   {edge_metrics.cv:.3f}")
        print(f"    95th pctl (L95):     {edge_metrics.l95:.2f}s")
        print(f"    SubLQ (perceptual):  {edge_metrics.sublq:.3f}")
    else:
        print("  No edge latency data (video repr not available)")

    print()
    cdn_metrics = compute_all(per_seg_cdn)
    print("  CDN buffer (fetch_time - video_media_time):")
    print(f"    Mean:                {cdn_metrics.mu_l:.2f}s")
    print(f"    Std deviation:       {cdn_metrics.sigma_l:.2f}s")
    print(f"    Jitter:              {cdn_metrics.j_l:.2f}s")
    print("=" * 60)


async def _run_youtube(args: argparse.Namespace) -> None:
    """Handle a YouTube live stream scraping session."""
    import re as _re
    # Derive a short channel name from the video ID for file naming
    m = _re.search(r"[?&]v=([A-Za-z0-9_-]+)", args.youtube_url)
    video_id = m.group(1) if m else "youtube"

    config = SessionConfig(
        broadcaster="youtube",
        channel=video_id,
        duration_minutes=args.duration,
        output_dir=args.output_dir,
        youtube_url=args.youtube_url,
        record_av=args.record_av,
    )

    logger = SessionLogger(config)
    scraper = YouTubeScraper(config, logger)

    print(f"\nLive Subtitle Scraper — YouTube / {video_id}")
    print(f"URL:      {args.youtube_url}")
    print(f"Duration: {args.duration} minutes | Output: {logger.output_dir}")
    if args.record_av:
        print("Audio:    ON (MPEG-TS segments → audio.wav at session end)")
    print("Press Ctrl+C to stop early.\n")

    records = []
    try:
        async with httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/134.0.0.0 Safari/537.36"
                ),
            },
            follow_redirects=True,
        ) as client:
            records = await scraper.run(client)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\nInterrupted — saving collected data...")
        records = scraper._records

    logger.finalise(len(records))
    print_summary(records)


async def main() -> None:
    args = parse_args()

    if args.list_channels:
        print("Available channels:\n")
        for cid, (bcast, name) in sorted(ALL_CHANNELS.items()):
            print(f"  {cid:<20s} {bcast.upper()} — {name}")
        sys.exit(0)

    if args.youtube_url:
        # YouTube path — short-circuit to a dedicated handler
        await _run_youtube(args)
        return

    if not args.channel:
        print("Error: --channel is required. Use --list-channels to see options.", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy per-request httpx logs unless verbose
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    # Auto-detect broadcaster from channel
    broadcaster = args.broadcaster or ALL_CHANNELS[args.channel][0]
    broadcaster_info = BROADCASTERS[broadcaster]
    valid_channels = broadcaster_info["channels"]

    config = SessionConfig(
        broadcaster=broadcaster,
        channel=args.channel,
        duration_minutes=args.duration,
        output_dir=args.output_dir,
        record_av=args.record_av,
        itv_mpd_url=args.mpd_url if broadcaster == "itv" else None,
        c4_mpd_url=args.mpd_url if broadcaster == "channel4" else None,
        amazon_mpd_url=args.mpd_url if broadcaster == "amazon" else None,
    )

    # Default headers per broadcaster
    _HEADERS = {
        "bbc": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.bbc.co.uk",
            "Referer": "https://www.bbc.co.uk/",
        },
        "itv": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.itv.com",
            "Referer": "https://www.itv.com/",
        },
        "channel4": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.channel4.com",
            "Referer": "https://www.channel4.com/",
        },
        "amazon": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.amazon.co.uk",
            "Referer": "https://www.amazon.co.uk/",
        },
    }
    session_headers = _HEADERS.get(broadcaster, _HEADERS["bbc"])

    audio_recorder = None  # Set when --record-av + --auto

    if args.auto and broadcaster == "bbc":
        from src.browser import capture_session
        print(f"Auto-capturing session for {args.channel}...")
        session, audio_recorder = await capture_session(
            channel=args.channel,
            headless=args.headless,
            record_audio=args.record_av,
            live_url=args.live_url,
        )
        config.bbc_x_param = session.x_param
        config.channel = session.channel
        config.bbc_video_repr = session.video_repr
        config.bbc_video_base_url = session.video_base_url
        # Akamai CDN often 403s outside the browser; prefer video CDN for all requests
        if session.video_base_url and session.video_base_url != session.base_url:
            log.info(
                "Subtitle CDN (%s) differs from video CDN (%s) — using video CDN for all",
                session.base_url, session.video_base_url,
            )
            config.bbc_base_url = session.video_base_url
        else:
            config.bbc_base_url = session.base_url

        # Use all headers the browser sent (excluding hop-by-hop headers)
        _skip = {"host", "connection", "content-length", "transfer-encoding"}
        session_headers = {
            k: v for k, v in session.headers.items()
            if k.lower() not in _skip
        }
        video_status = f"video_repr={session.video_repr}" if session.video_repr else "no video repr captured"
        print(f"Session captured:")
        print(f"  CDN:     {config.bbc_base_url}")
        print(f"  x=       {session.x_param}")
        print(f"  channel: {session.channel}")
        print(f"  {video_status}")
        if audio_recorder:
            print(f"  Audio recording: ON")
        log.debug("Session headers: %s", session_headers)
    elif args.auto and broadcaster == "itv":
        from src.browser import capture_itv_session
        print(f"Auto-capturing session for {args.channel}...")
        itv_session, audio_recorder = await capture_itv_session(
            channel=args.channel,
            headless=args.headless,
            record_audio=args.record_av,
        )
        config.itv_mpd_url = itv_session.mpd_url

        _skip = {"host", "connection", "content-length", "transfer-encoding"}
        session_headers = {
            k: v for k, v in itv_session.headers.items()
            if k.lower() not in _skip
        }
        print(f"Session captured:")
        print(f"  MPD: {itv_session.mpd_url[:100]}...")
        if audio_recorder:
            print(f"  Audio recording: ON")
        log.debug("Session headers: %s", session_headers)
    elif args.auto and broadcaster == "channel4":
        from src.browser import capture_c4_session
        print(f"Auto-capturing session for {args.channel}...")
        c4_session, audio_recorder = await capture_c4_session(
            channel=args.channel,
            headless=args.headless,
            record_audio=args.record_av,
            live_url=args.live_url,
        )
        config.c4_mpd_url = c4_session.mpd_url

        _skip = {"host", "connection", "content-length", "transfer-encoding"}
        session_headers = {
            k: v for k, v in c4_session.headers.items()
            if k.lower() not in _skip
        }
        print(f"Session captured:")
        print(f"  MPD: {c4_session.mpd_url[:100]}...")
        if c4_session.subtitle_url:
            print(f"  Subtitle sidecar: {c4_session.subtitle_url[:100]}...")
            config.c4_subtitle_url = c4_session.subtitle_url
        if audio_recorder:
            print(f"  Audio recording: ON")
        log.debug("Session headers: %s", session_headers)
    elif args.auto and broadcaster == "amazon":
        from src.browser import capture_amazon_session
        print(f"Auto-capturing session for {args.channel}...")
        amazon_session, audio_recorder = await capture_amazon_session(
            channel=args.channel,
            headless=args.headless,
            record_audio=args.record_av,
            live_url=args.live_url,
            wait_for_enter=args.wait_for_enter,
        )
        config.amazon_mpd_url = amazon_session.mpd_url

        _skip = {"host", "connection", "content-length", "transfer-encoding"}
        session_headers = {
            k: v for k, v in amazon_session.headers.items()
            if k.lower() not in _skip
        }
        print(f"Session captured:")
        print(f"  MPD: {amazon_session.mpd_url[:100]}...")
        if audio_recorder:
            print(f"  Audio recording: ON")
        log.debug("Session headers: %s", session_headers)
    else:
        # Manual mode overrides
        if args.base_url:
            config.bbc_base_url = args.base_url
        if args.x_param is not None:
            config.bbc_x_param = args.x_param

    logger = SessionLogger(config)
    scraper_cls = broadcaster_info["scraper_cls"]
    scraper = scraper_cls(config, logger)

    # Start audio recording now that we know the output directory
    if audio_recorder:
        await audio_recorder.start(logger.output_dir / "audio")
        if audio_recorder.audio_start_unix:
            logger.save_audio_start(
                audio_recorder.audio_start_unix,
                audio_recorder.video_time_at_start,
                video_buffered_end=getattr(audio_recorder, 'video_buffered_end', None),
                stream_latency=getattr(audio_recorder, 'stream_latency', None),
            )
            # Tell the scraper when audio started so it only saves
            # subtitle cues that overlap with the audio timeline
            scraper.audio_start_unix = audio_recorder.audio_start_unix

    print(f"\nLive Subtitle Scraper — {broadcaster.upper()} / {valid_channels[config.channel]}")
    print(f"Duration: {args.duration} minutes | Output: {logger.output_dir}")
    print("Press Ctrl+C to stop early.\n")

    records = []
    try:
        async with httpx.AsyncClient(
            headers=session_headers,
            follow_redirects=True,
        ) as client:
            records = await scraper.run(client)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\nInterrupted — saving collected data...")
        records = scraper._records
    finally:
        # Stop audio recording and save if active.
        # Shield from further cancellation so the audio is always saved.
        if audio_recorder:
            try:
                print("\nStopping audio recording...")
                audio_path = await asyncio.shield(audio_recorder.stop_and_save())
                # Save calibration beep offset (collected during stop).
                # Use the logger's current video_time (may have been corrected
                # by the scraper with the DASH presentation time) rather than
                # the audio_recorder's original value.
                if audio_recorder.audio_start_unix is not None:
                    logger.save_audio_start(
                        audio_recorder.audio_start_unix,
                        getattr(audio_recorder, 'video_time_at_start', None)
                            if config.broadcaster == "amazon"
                            and getattr(audio_recorder, 'video_time_at_start', None) is not None
                            else getattr(logger, '_video_time_at_start', audio_recorder.video_time_at_start),
                        audio_recorder.calibration_beep_offset,
                        video_buffered_end=getattr(audio_recorder, 'video_buffered_end', None)
                            if getattr(audio_recorder, 'video_buffered_end', None) is not None
                            else getattr(logger, '_video_buffered_end', None),
                        stream_latency=getattr(audio_recorder, 'stream_latency', None)
                            if getattr(audio_recorder, 'stream_latency', None) is not None
                            else getattr(logger, '_stream_latency', None),
                        mse_timestamp_offset=getattr(audio_recorder, '_mse_timestamp_offset', None),
                        video_source_frame=getattr(audio_recorder, '_video_source_frame', None),
                        video_num_candidates=getattr(audio_recorder, '_video_num_candidates', None),
                        stream_latency_method=getattr(audio_recorder, 'stream_latency_method', None),
                    )
                if audio_path:
                    print(f"Audio saved: {audio_path}")
            except Exception as exc:
                log.error("Failed to save audio recording: %s", exc)
            finally:
                try:
                    await audio_recorder.close()
                except Exception:
                    pass

    logger.finalise(len(records))
    print_summary(records)


if __name__ == "__main__":
    asyncio.run(main())
