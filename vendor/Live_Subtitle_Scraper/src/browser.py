"""Browser-based session bootstrapper using Playwright.

Opens a live stream in a real Chromium instance, intercepts subtitle-related
network requests, and extracts session parameters. When --record-av is used,
the browser stays open to record tab audio via MediaRecorder.

Supports:
  - BBC iPlayer: captures subtitle .m4s URL → extracts CDN base, x= param
  - ITV (ITVX): captures .mpd manifest URL and request headers
  - Channel 4: captures .mpd manifest URL (skips Yospace ad-insertion URLs)
  - Audio recording: captures browser tab audio as WAV via MediaRecorder + ffmpeg
"""

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Request, Page

log = logging.getLogger(__name__)

# Matches BBC subtitle segment URLs like:
# .../x=4/i=urn:bbc:pips:service:bbc_one_london/t=3840/s=caption1/b=64000/462075226.m4s
_SUBTITLE_URL_RE = re.compile(
    r"(?P<base>https?://[^/]+)"
    r"/x=(?P<x>\d+)"
    r"/i=urn:bbc:pips:service:(?P<channel>[^/]+)"
    r"/t=3840/s=caption1/b=64000"
    r"/(?P<segment>\d+)\.m4s"
)

# Matches BBC video segment URLs like:
# .../x=4/i=urn:bbc:pips:service:bbc_one_london/t=50/v=pv14/b=5070016/462075226.m4s
_VIDEO_URL_RE = re.compile(
    r"(?P<base>https?://[^/]+)"
    r"/x=(?P<x>\d+)"
    r"/i=urn:bbc:pips:service:(?P<channel>[^/]+)"
    r"/(?P<video_repr>t=\d+/v=[^/]+/b=\d+)"
    r"/(?P<segment>\d+)\.m4s"
)


BBC_IPLAYER_LIVE_URLS = {
    "bbc_one_london": "https://www.bbc.co.uk/iplayer/live/bbcone",
    "bbc_one_hd": "https://www.bbc.co.uk/iplayer/live/bbcone",
    "bbc_two_hd": "https://www.bbc.co.uk/iplayer/live/bbctwo",
    "bbc_two_northern_ireland_hd": "https://www.bbc.co.uk/iplayer/live/bbctwo",
    "bbc_two_wales_digital": "https://www.bbc.co.uk/iplayer/live/bbctwo",
    "bbc_news24": "https://www.bbc.co.uk/iplayer/live/bbcnews",
    "bbc_three_hd": "https://www.bbc.co.uk/iplayer/live/bbcthree",
    "bbc_one_scotland_hd": "https://www.bbc.co.uk/iplayer/live/bbcone",
    "bbc_one_wales_hd": "https://www.bbc.co.uk/iplayer/live/bbcone",
    "bbc_one_northern_ireland_hd": "https://www.bbc.co.uk/iplayer/live/bbcone",
}


@dataclass
class CapturedSession:
    """Session parameters extracted from a live iPlayer subtitle request."""
    base_url: str
    x_param: int
    channel: str
    headers: dict[str, str]
    video_repr: str | None = None  # e.g. "t=3840/v=pv10/b=1604032"
    video_base_url: str | None = None  # may differ from subtitle CDN


@dataclass
class CapturedITVSession:
    """Session parameters extracted from ITVX network traffic."""
    mpd_url: str
    headers: dict[str, str]
    subtitle_url: str | None = None  # sidecar VTT/TTML URL (VOD only)


@dataclass
class CapturedAmazonSession:
    """Session parameters extracted from Amazon Prime Video network traffic."""
    mpd_url: str
    headers: dict[str, str]


# --- Audio recording via tab capture (getDisplayMedia) ---

# JS that sets up the recording function — called from a click handler
# so Chrome accepts it as having a user gesture.
_SETUP_CAPTURE_JS = """
() => {
    window.__captureResult = null;
    window.__pendingChunks = [];  // base64 chunks ready for collection
    window.__startCapture = async () => {
        try {
            const stream = await navigator.mediaDevices.getDisplayMedia({
                video: {width: 1, height: 1, frameRate: 1},
                audio: true,
                preferCurrentTab: true,
                selfBrowserSurface: 'include',
            });
            stream.getVideoTracks().forEach(t => { t.stop(); stream.removeTrack(t); });
            if (stream.getAudioTracks().length === 0) {
                window.__captureResult = 'error: no audio tracks';
                return;
            }
            window.__recorder = new MediaRecorder(stream, {mimeType: 'audio/webm;codecs=opus'});
            // Convert each chunk to base64 immediately so Python can
            // periodically collect and flush to disk. This avoids
            // accumulating hours of audio in browser memory.
            window.__recorder.ondataavailable = e => {
                if (e.data.size > 0) {
                    const reader = new FileReader();
                    reader.onloadend = () => {
                        const b64 = reader.result.split(',')[1];
                        window.__pendingChunks.push(b64);
                    };
                    reader.readAsDataURL(e.data);
                }
            };
            window.__recorder.start(1000);
            // Capture the video's media position at recording start
            // so we can map audio time → media timeline.
            // BBC iPlayer uses Shadow DOM, so we search recursively.
            function findVideo(root) {
                const v = root.querySelector('video');
                if (v) return v;
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        const found = findVideo(el.shadowRoot);
                        if (found) return found;
                    }
                }
                return null;
            }
            const video = findVideo(document);
            window.__videoTimeAtStart = video ? video.currentTime : null;
            window.__wallClockAtStart = Date.now() / 1000;

            // Calibration: inject a short tone into the tab audio at a known
            // wall-clock offset. By detecting the tone in the recorded audio,
            // we measure the exact MediaRecorder pipeline delay.
            // We use 6000Hz (above speech, below 8kHz Nyquist for 16kHz WAV).
            window.__recorderStartPerf = performance.now();
            window.__calibrationBeepOffset = null;
            setTimeout(() => {
                try {
                    const actx = new AudioContext();
                    const osc = actx.createOscillator();
                    const gain = actx.createGain();
                    osc.frequency.value = 6000;
                    gain.gain.value = 0.3;
                    osc.connect(gain);
                    gain.connect(actx.destination);
                    osc.start();
                    window.__calibrationBeepOffset = (performance.now() - window.__recorderStartPerf) / 1000;
                    setTimeout(() => {
                        osc.stop();
                        actx.close();
                    }, 150);
                } catch(e) {}
            }, 2000);  // 2s after recording start
            window.__captureResult = 'recording';
        } catch(e) {
            window.__captureResult = 'error: ' + e.message;
        }
    };
    // Create an invisible button that triggers capture on click
    const btn = document.createElement('button');
    btn.id = '__audio_capture_trigger';
    btn.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0.01;z-index:999999;';
    btn.addEventListener('click', () => window.__startCapture());
    document.body.appendChild(btn);
    return 'ready';
}
"""

_RECORDER_STOP_JS = """
() => new Promise(resolve => {
    if (!window.__recorder || window.__recorder.state === 'inactive') {
        resolve('already_stopped');
        return;
    }
    window.__recorder.onstop = () => resolve('stopped');
    window.__recorder.stop();
})
"""

# Drain pending base64 chunks from JS — returns array of b64 strings and clears the buffer
_DRAIN_CHUNKS_JS = """
() => {
    const chunks = window.__pendingChunks || [];
    window.__pendingChunks = [];
    return chunks;
}
"""

# Extra Chrome flags for tab capture permissions
_AUDIO_CAPTURE_CHROME_ARGS = [
    "--enable-usermedia-screen-capturing",
]


class BrowserAudioRecorder:
    """Records Chrome tab audio using getDisplayMedia + MediaRecorder.

    Uses getDisplayMedia({preferCurrentTab: true}) to capture audio at the
    tab compositor level, bypassing the EME/DRM restriction that blocks
    captureStream() on encrypted media elements.

    The getDisplayMedia API requires a user gesture (click). We inject a
    hidden button and use Playwright's page.click() to provide a real
    user gesture that Chrome accepts.

    Usage:
        recorder = BrowserAudioRecorder(page, chrome_proc)
        await recorder.start(Path("output/session/audio.wav"))
        # ... scraper runs ...
        await recorder.stop_and_save()
        await recorder.close()
    """

    def __init__(self, page: Page, chrome_proc: subprocess.Popen | None = None,
                 browser=None, pw_context=None):
        self._page = page
        self._chrome_proc = chrome_proc
        self._browser = browser
        self._pw_context = pw_context
        self._recording = False
        self._wav_path: Path | None = None
        self._webm_path: Path | None = None  # temp file for accumulated WebM chunks
        self._flush_task: asyncio.Task | None = None
        self._timing_task: asyncio.Task | None = None
        self._flush_interval_s = 30  # collect chunks from browser every 30s
        self.audio_start_unix: float | None = None   # wall clock when recording began
        self.video_time_at_start: float | None = None # video.currentTime when recording began
        self.video_buffered_end: float | None = None
        self.calibration_beep_offset: float | None = None  # wall-clock offset of calibration tone
        self.stream_latency: float | None = None  # seconds between live edge and browser playback
        self.stream_latency_method: str | None = None
        self._video_segment_requests: list[tuple[float, str]] = []  # set by Amazon capture
        self._amazon_mpd_url: str | None = None
        self._amazon_headers: dict | None = None
        self._timing_trace_path: Path | None = None
        self._best_timing_state: dict[str, object] | None = None

    async def start(self, output_path: Path) -> None:
        """Start tab audio capture via getDisplayMedia triggered by a click."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._wav_path = output_path.with_suffix(".wav")
        self._timing_trace_path = output_path.parent / "player_timing.jsonl"

        try:
            log.info("Waiting for <video> element before starting audio capture...")
            await self._page.wait_for_selector("video", state="attached", timeout=30_000)
            await asyncio.sleep(3)

            # Grant displayCapture permission via CDP
            try:
                cdp = await self._page.context.new_cdp_session(self._page)
                origin = self._page.url.split("?")[0].rsplit("/", 1)[0]
                await cdp.send("Browser.grantPermissions", {
                    "permissions": ["displayCapture"],
                    "origin": origin,
                })
                log.info("Granted displayCapture permission for %s", origin)
            except Exception as exc:
                log.debug("CDP permission grant attempt: %s", exc)

            # Inject capture function + hidden trigger button
            await self._page.evaluate(_SETUP_CAPTURE_JS)

            # Click the button — gives getDisplayMedia a real user gesture
            await self._page.click("#__audio_capture_trigger")

            # Wait for capture to complete (dialog auto-approved by Chrome flags)
            for _ in range(20):
                await asyncio.sleep(0.5)
                result = await self._page.evaluate("() => window.__captureResult")
                if result:
                    break

            if result == "recording":
                import time
                self.audio_start_unix = time.time()
                # Log all open pages so we can see if the player is in a separate tab
                try:
                    pages_info = [(p.url[:80], len(p.frames)) for p in self._page.context.pages]
                    log.info("Open pages at recording start: %s", pages_info)
                except Exception:
                    pass
                # Re-inject MSE patch into the player page if it opened in a new tab
                if self._amazon_mpd_url:
                    try:
                        for pg in self._page.context.pages:
                            for frame in pg.frames:
                                try:
                                    await frame.evaluate("""
                                        (() => {
                                            if (window.__msePatchApplied) return 'already';
                                            window.__msePatchApplied = true;
                                            const origCreateObjectURL = URL.createObjectURL.bind(URL);
                                            URL.createObjectURL = function(obj) {
                                                const url = origCreateObjectURL(obj);
                                                if (obj instanceof MediaSource) {
                                                    if (!window.__capturedMediaSources) window.__capturedMediaSources = [];
                                                    window.__capturedMediaSources.push(obj);
                                                }
                                                return url;
                                            };
                                            const origAddSB = MediaSource.prototype.addSourceBuffer;
                                            MediaSource.prototype.addSourceBuffer = function(mimeType) {
                                                const sb = origAddSB.call(this, mimeType);
                                                if (!window.__capturedSourceBuffers) window.__capturedSourceBuffers = [];
                                                window.__capturedSourceBuffers.push({sb, mimeType});
                                                const proto = sb.__proto__.__proto__;
                                                const descriptor = Object.getOwnPropertyDescriptor(proto, 'timestampOffset')
                                                    || Object.getOwnPropertyDescriptor(sb.__proto__, 'timestampOffset');
                                                if (descriptor && descriptor.set) {
                                                    const origSet = descriptor.set;
                                                    const origGet = descriptor.get;
                                                    Object.defineProperty(sb, 'timestampOffset', {
                                                        get() { return origGet.call(this); },
                                                        set(val) {
                                                            origSet.call(this, val);
                                                            if (!window.__mseTimestampOffsets) window.__mseTimestampOffsets = {};
                                                            window.__mseTimestampOffsets[mimeType] = val;
                                                        },
                                                        configurable: true,
                                                    });
                                                }
                                                return sb;
                                            };
                                            return 'patched';
                                        })()
                                    """)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                # Inject MSE patch into ALL targets via raw CDP (bypasses
                # same-origin restrictions that block frame.evaluate).
                if self._amazon_mpd_url:
                    await self._inject_mse_patch_via_cdp()

                # Read the video's media position captured by JS (may be from a
                # main-page preview video, not the actual player in an iframe).
                self.video_time_at_start = await self._page.evaluate(
                    "() => window.__videoTimeAtStart"
                )
                # Try Playwright frames first, then CDP targets as fallback.
                await self._capture_video_timing()
                if self.video_time_at_start is None or self.video_time_at_start == 0:
                    await asyncio.sleep(2)
                    await self._capture_video_timing(force=True)
                # Also read via raw CDP to reach cross-origin iframes Playwright can't see.
                if self._amazon_mpd_url:
                    await self._refresh_video_timing_from_cdp()
                # Capture buffered end time (for computing MSE timestamp offset)
                # Compute stream latency from video segment tfdt (Amazon only)
                await self._compute_stream_latency()
                if self._amazon_mpd_url:
                    self._record_timing_sample("start")
                # Save a DASH audio segment for cross-correlation calibration
                if self._amazon_mpd_url:
                    await self._save_dash_audio_reference(output_path.parent)
                self._recording = True
                # Start periodic flush task to drain chunks to disk
                self._webm_path = self._wav_path.with_suffix(".webm")
                self._flush_task = asyncio.create_task(self._periodic_flush())
                if self._amazon_mpd_url:
                    self._timing_task = asyncio.create_task(self._monitor_amazon_timing())
                log.info(
                    "Tab audio recording started | wall=%.3f video.currentTime=%.3f buffered.end=%s",
                    self.audio_start_unix,
                    self.video_time_at_start or 0,
                    f"{self.video_buffered_end:.3f}" if self.video_buffered_end else "N/A",
                )
            else:
                log.warning("Tab audio capture failed: %s", result)
        except Exception as exc:
            log.warning("Failed to start tab audio recording: %s", exc)

    def _timing_method_priority(self, method: str | None) -> int:
        return {
            "mse_timestamp_offset": 3,
            "tfdt_plus_buffer": 2,
            "tfdt_cdn_only": 1,
        }.get(method or "", 0)

    def _snapshot_timing_state(self) -> dict[str, object]:
        return {
            "video_time_at_start": self.video_time_at_start,
            "video_buffered_end": self.video_buffered_end,
            "stream_latency": self.stream_latency,
            "stream_latency_method": self.stream_latency_method,
            "mse_timestamp_offset": getattr(self, "_mse_timestamp_offset", None),
            "video_source_frame": getattr(self, "_video_source_frame", None),
            "video_num_candidates": getattr(self, "_video_num_candidates", None),
        }

    def _restore_timing_state(self, state: dict[str, object]) -> None:
        self.video_time_at_start = state.get("video_time_at_start")  # type: ignore[assignment]
        self.video_buffered_end = state.get("video_buffered_end")  # type: ignore[assignment]
        self.stream_latency = state.get("stream_latency")  # type: ignore[assignment]
        self.stream_latency_method = state.get("stream_latency_method")  # type: ignore[assignment]
        self._mse_timestamp_offset = state.get("mse_timestamp_offset")
        self._video_source_frame = state.get("video_source_frame")
        self._video_num_candidates = state.get("video_num_candidates")

    async def _get_amazon_ast(self) -> float | None:
        from src.parsers.mpd import get_mpd_availability_start

        ast = getattr(self, "_availability_start_time", None)
        if ast is not None or not self._amazon_mpd_url:
            return ast

        try:
            import httpx

            headers = dict(self._amazon_headers) if self._amazon_headers else {}
            headers.pop("host", None)
            async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                mpd_resp = await client.get(self._amazon_mpd_url, timeout=8.0)
                ast = get_mpd_availability_start(mpd_resp.content)
                self._availability_start_time = ast
                return ast
        except Exception as exc:
            log.debug("Failed to fetch Amazon AST for timing: %s", exc)
            return None

    async def _refresh_video_timing_from_cdp(self, force: bool = False) -> None:
        """Promote the best CDP video timing candidate into recorder state."""
        cdp_candidates = await self._read_video_info_via_cdp()
        if not cdp_candidates:
            return

        ast = await self._get_amazon_ast()

        def _cdp_score(candidate):
            vct, buf_end, ts_offset, _url = candidate
            buffer_ahead = (buf_end - vct) if (buf_end is not None and vct > 0) else 0
            ts_valid = 0
            if ts_offset is not None:
                pd = vct - ts_offset
                audio_s = self.audio_start_unix or 0
                if ast is not None:
                    lat_a = audio_s - (ast + pd)
                    if -5 < lat_a < 60:
                        ts_valid = 1
                if not ts_valid:
                    lat_b = audio_s - pd
                    if -5 < lat_b < 60:
                        ts_valid = 1
                if not ts_valid:
                    log.debug(
                        "CDP: rejecting tsOffset=%.3f for vct=%.3f "
                        "(A=%.1fs, B=%.1fs, both outside -5..60)",
                        ts_offset, vct,
                        audio_s - (ast + pd) if ast is not None else float("nan"),
                        audio_s - pd,
                    )
            return (ts_valid, buffer_ahead)

        best = max(cdp_candidates, key=_cdp_score)
        best_score = _cdp_score(best)
        if (
            force
            or self.video_time_at_start is None
            or self.video_time_at_start == 0
            or best_score[0] == 1
        ):
            self.video_time_at_start = best[0]
            self.video_buffered_end = best[1]
            self._mse_timestamp_offset = best[2] if best_score[0] == 1 else None
            self._video_source_frame = best[3] + " (CDP)"
            self._video_num_candidates = len(cdp_candidates)
            log.info(
                "Using CDP video candidate: vct=%.3f tsOffset=%s from %s",
                best[0],
                f"{best[2]:.3f}" if best[2] is not None else "N/A",
                best[3][:60],
            )

    def _record_timing_sample(self, note: str) -> None:
        if not self._timing_trace_path or self.audio_start_unix is None:
            return

        import time as _time

        now = _time.time()
        state = {**self._snapshot_timing_state(), "note": note}
        priority = self._timing_method_priority(self.stream_latency_method)
        best_priority = self._timing_method_priority(
            self._best_timing_state.get("stream_latency_method") if self._best_timing_state else None
        )

        promoted = False
        can_promote = (
            note == "start"
            or self.stream_latency_method == "mse_timestamp_offset"
            or self._best_timing_state is None
        )
        if self.stream_latency is not None and can_promote and (
            self._best_timing_state is None
            or priority > best_priority
            or (
                priority == best_priority
                and state.get("mse_timestamp_offset") is not None
                and self._best_timing_state.get("mse_timestamp_offset") is None
            )
            or (
                priority == best_priority
                and note == "start"
                and self._best_timing_state.get("note") != "start"
            )
        ):
            self._best_timing_state = state
            promoted = True

        payload = {
            "wall_unix": now,
            "audio_elapsed_s": now - self.audio_start_unix,
            "promoted": promoted,
            **state,
        }
        with open(self._timing_trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    async def _monitor_amazon_timing(self) -> None:
        """Probe for a better player-timing anchor shortly after recording starts."""
        try:
            for attempt in range(12):
                if not self._recording:
                    return
                await asyncio.sleep(2 if attempt < 8 else 5)
                await self._capture_video_timing(force=True)
                await self._refresh_video_timing_from_cdp(force=True)
                await self._compute_stream_latency()
                self._record_timing_sample(f"monitor_{attempt + 1}")
                if self._timing_method_priority(self.stream_latency_method) >= 3:
                    break
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.debug("Amazon timing monitor failed: %s", exc)
        finally:
            if self._best_timing_state is not None:
                self._restore_timing_state(self._best_timing_state)

    async def _drain_and_write(self) -> int:
        """Drain pending base64 chunks from the browser and append to the WebM file.

        Returns the number of chunks written.
        """
        import base64

        try:
            chunks = await self._page.evaluate(_DRAIN_CHUNKS_JS)
        except Exception as exc:
            log.debug("Failed to drain chunks from browser: %s", exc)
            return 0

        if not chunks:
            return 0

        with open(self._webm_path, "ab") as f:
            for b64 in chunks:
                f.write(base64.b64decode(b64))

        return len(chunks)

    async def _periodic_flush(self) -> None:
        """Background task that periodically drains audio chunks to disk."""
        try:
            while self._recording:
                await asyncio.sleep(self._flush_interval_s)
                n = await self._drain_and_write()
                if n > 0:
                    size_mb = self._webm_path.stat().st_size / 1024 / 1024
                    log.info("Flushed %d audio chunks to disk (%.1f MB total)", n, size_mb)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("Periodic flush task ended: %s", exc)

    async def _inject_mse_patch_via_cdp(self) -> None:
        """Inject MSE patch into every browser target via raw CDP.

        Playwright's frame.evaluate() can't cross origin boundaries even with
        site isolation disabled. But CDP's Runtime.evaluate on a session
        attached directly to a specific target CAN — it bypasses JS same-origin
        restrictions entirely because it goes through the browser protocol layer.

        This reaches cross-origin iframes (like the Amazon player) that
        Playwright's frame abstraction cannot.
        """
        _PATCH_EXPR = """
            (() => {
                if (window.__msePatchApplied) return 'already';
                window.__msePatchApplied = true;
                const origCreateObjectURL = URL.createObjectURL.bind(URL);
                URL.createObjectURL = function(obj) {
                    const url = origCreateObjectURL(obj);
                    if (obj instanceof MediaSource) {
                        if (!window.__capturedMediaSources) window.__capturedMediaSources = [];
                        window.__capturedMediaSources.push(obj);
                    }
                    return url;
                };
                const origAddSB = MediaSource.prototype.addSourceBuffer;
                MediaSource.prototype.addSourceBuffer = function(mimeType) {
                    const sb = origAddSB.call(this, mimeType);
                    if (!window.__capturedSourceBuffers) window.__capturedSourceBuffers = [];
                    window.__capturedSourceBuffers.push({sb, mimeType});
                    const proto = sb.__proto__.__proto__;
                    const descriptor = Object.getOwnPropertyDescriptor(proto, 'timestampOffset')
                        || Object.getOwnPropertyDescriptor(sb.__proto__, 'timestampOffset');
                    if (descriptor && descriptor.set) {
                        const origSet = descriptor.set;
                        const origGet = descriptor.get;
                        Object.defineProperty(sb, 'timestampOffset', {
                            get() { return origGet.call(this); },
                            set(val) {
                                origSet.call(this, val);
                                if (!window.__mseTimestampOffsets) window.__mseTimestampOffsets = {};
                                window.__mseTimestampOffsets[mimeType] = val;
                            },
                            configurable: true,
                        });
                    }
                    return sb;
                };
                return 'patched';
            })()
        """
        try:
            # Get a browser-level CDP session to enumerate all targets
            root_cdp = await self._page.context.new_cdp_session(self._page)
            result = await root_cdp.send("Target.getTargets")
            targets = result.get("targetInfos", [])
            log.debug("CDP targets: %s", [(t.get("type"), t.get("url", "")[:60]) for t in targets])

            for target in targets:
                target_type = target.get("type", "")
                target_url = target.get("url", "")
                if target_type not in ("page", "iframe"):
                    continue

                target_id = target.get("targetId")
                try:
                    # Attach to this target with a flat CDP session
                    attach_result = await root_cdp.send("Target.attachToTarget", {
                        "targetId": target_id,
                        "flatten": True,
                    })
                    session_id = attach_result.get("sessionId")
                    if not session_id:
                        continue

                    # Evaluate the patch in this target's JS context
                    eval_result = await root_cdp.send("Runtime.evaluate", {
                        "expression": _PATCH_EXPR,
                        "sessionId": session_id,
                    })
                    patch_result = eval_result.get("result", {}).get("value", "error")
                    log.debug("CDP patch in %s (%s): %s", target_type, target_url[:60], patch_result)

                    await root_cdp.send("Target.detachFromTarget", {"sessionId": session_id})
                except Exception as exc:
                    log.debug("CDP patch failed for target %s (%s): %s", target_id, target_url[:60], exc)
        except Exception as exc:
            log.debug("CDP MSE patch injection failed: %s", exc)

    async def _read_video_info_via_cdp(self) -> list[tuple[float, float | None, float | None, str]]:
        """Read video timing from all Chrome tabs via per-tab WebSocket CDP connections.

        Uses /json/list HTTP endpoint to enumerate every tab (including those
        Playwright doesn't track), then connects to each tab's own WebSocket
        debugger URL.  This avoids conflicts with Playwright's browser-level
        CDP connection.

        Critically, uses Runtime.queryObjects(MediaSource.prototype) to find
        ALL MediaSource instances in the JS heap — no monkey-patch needed, works
        on already-running players.  Each MediaSource is paired with its video
        element by matching blob URLs, so currentTime and timestampOffset always
        come from the same player.

        Returns list of (adj_vct, buffered_end, ts_offset, tab_url) candidates.
        """
        import time as _time
        import aiohttp as _aiohttp
        import json as _json

        # JS to extract video+MediaSource pairs from one tab.
        # Uses Runtime.evaluate — works in any tab context.
        _VIDEO_AND_MS_JS = r"""
            (() => {
                // Collect all playing video elements (including shadow DOM).
                function collectVideos(root, out) {
                    for (const v of root.querySelectorAll('video')) {
                        if (v.readyState >= 2 && v.currentTime > 0) out.push(v);
                    }
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) collectVideos(el.shadowRoot, out);
                    }
                }
                const vids = [];
                collectVideos(document, vids);
                if (!vids.length) return null;

                // Build map: blobUrl → {tsOffset, bufSizes}
                // We walk all open MediaSources via window.__capturedMediaSources
                // (set by our init-script patch if it fired) OR via __capturedSourceBuffers.
                // This gives us the tsOffset paired with the MediaSource that owns it.
                const msMap = new Map(); // blobUrl → tsOffset
                try {
                    for (const ms of (window.__capturedMediaSources || [])) {
                        if (ms.readyState === 'closed') continue;
                        let tsOffset = null;
                        for (let i = 0; i < ms.sourceBuffers.length; i++) {
                            const sb = ms.sourceBuffers[i];
                            const val = sb.timestampOffset;
                            if (val !== undefined && val !== 0) {
                                tsOffset = val; break;
                            }
                        }
                        // We stored the blob URL in __msBlobUrls when creating it
                        const blobUrl = (window.__msBlobUrls || {})[ms.__id__];
                        if (blobUrl !== undefined) msMap.set(blobUrl, tsOffset);
                        else if (tsOffset !== null) msMap.set('__any__', tsOffset);
                    }
                } catch(e) {}
                // Also check __mseTimestampOffsets from setter interceptor
                const patchedOffsets = window.__mseTimestampOffsets || null;

                const results = vids.map(v => {
                    // Try to get tsOffset paired with this specific video's src
                    let tsOffset = null;
                    try {
                        if (msMap.has(v.src)) tsOffset = msMap.get(v.src);
                        else if (msMap.has('__any__')) tsOffset = msMap.get('__any__');
                        // srcObject path (non-blob)
                        if (tsOffset === null && v.srcObject && v.srcObject.sourceBuffers) {
                            for (let i = 0; i < v.srcObject.sourceBuffers.length; i++) {
                                const val = v.srcObject.sourceBuffers[i].timestampOffset;
                                if (val !== undefined && val !== 0) { tsOffset = val; break; }
                            }
                        }
                        // Fallback to setter interceptor values (may be from wrong MS)
                        if (tsOffset === null && patchedOffsets) {
                            for (const [mime, val] of Object.entries(patchedOffsets)) {
                                if (mime.includes('video') && val !== 0) { tsOffset = val; break; }
                            }
                        }
                    } catch(e) {}
                    return {
                        currentTime: v.currentTime,
                        bufferedEnd: v.buffered.length > 0
                            ? v.buffered.end(v.buffered.length - 1) : null,
                        timestampOffset: tsOffset,
                        src: v.src ? v.src.substring(0, 40) : '',
                    };
                });
                return { videos: results };
            })()
        """

        candidates = []
        capture_wall = _time.time()
        debug_port = getattr(self, '_debug_port', 9224)

        try:
            async with _aiohttp.ClientSession() as http:
                r = await http.get(f"http://localhost:{debug_port}/json/list", timeout=_aiohttp.ClientTimeout(total=3))
                tabs = await r.json(content_type=None)
        except Exception as exc:
            log.debug("CDP /json/list failed: %s", exc)
            return candidates

        log.debug("CDP /json/list: %d tabs", len(tabs))

        for tab in tabs:
            tab_type = tab.get("type", "")
            tab_url = tab.get("url", "")
            ws_url = tab.get("webSocketDebuggerUrl", "")
            if tab_type not in ("page",) or not ws_url:
                continue

            try:
                msg_id = 0

                async def _ws_call(ws, method, params=None):
                    nonlocal msg_id
                    msg_id += 1
                    await ws.send_str(_json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                    # Read responses until we see the one with our id (skip events)
                    _expected_id = msg_id
                    async for msg in ws:
                        if msg.type != _aiohttp.WSMsgType.TEXT:
                            continue  # skip ping/pong/close frames
                        data = _json.loads(msg.data)
                        if data.get("id") == _expected_id:
                            return data.get("result", {})
                        # keep reading; events (no id) and other responses are skipped
                    return {}

                async with _aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        ws_url,
                        timeout=_aiohttp.ClientTimeout(total=10, sock_read=3),
                    ) as ws:
                        # Evaluate the video+MS info JS in this tab
                        result = await _ws_call(ws, "Runtime.evaluate", {
                            "expression": _VIDEO_AND_MS_JS,
                            "returnByValue": True,
                            "awaitPromise": False,
                        })
                        result_data = result.get("result", {}).get("value")
                        if not result_data or not isinstance(result_data, dict):
                            log.debug("CDP tab %s: no video data", tab_url[:60])
                            continue

                        elapsed = capture_wall - (self.audio_start_unix or capture_wall)
                        vid_infos = result_data.get("videos", [])
                        vid_infos = [v for v in vid_infos if v.get("currentTime", 0) > 0]
                        if not vid_infos:
                            continue

                        # If the MSE patch didn't fire (tab was already open), all
                        # tsOffsets will be null.  Fall back to Runtime.queryObjects
                        # which enumerates ALL MediaSource instances in the V8 heap
                        # and reads timestampOffset directly — no monkey-patch needed.
                        any_ts_null = any(v.get("timestampOffset") is None for v in vid_infos)
                        qo_offsets: list[float] = []
                        if any_ts_null:
                            try:
                                # Step 1: get MediaSource.prototype objectId
                                proto_r = await _ws_call(ws, "Runtime.evaluate", {
                                    "expression": "MediaSource.prototype",
                                    "returnByValue": False,
                                })
                                proto_id = (proto_r.get("result") or {}).get("objectId")
                                if proto_id:
                                    # Step 2: enumerate all MediaSource instances
                                    qo_r = await _ws_call(ws, "Runtime.queryObjects", {
                                        "prototypeObjectId": proto_id,
                                    })
                                    ms_arr_id = (qo_r.get("objects") or {}).get("objectId")
                                    if ms_arr_id:
                                        # Step 3: read timestampOffset from each sourceBuffer
                                        fn_r = await _ws_call(ws, "Runtime.callFunctionOn", {
                                            "objectId": ms_arr_id,
                                            "functionDeclaration": (
                                                "function() {"
                                                "  const out = [];"
                                                "  for (const ms of this) {"
                                                "    if (ms.readyState === 'closed') continue;"
                                                "    for (let i = 0; i < ms.sourceBuffers.length; i++) {"
                                                "      const v = ms.sourceBuffers[i].timestampOffset;"
                                                "      if (v !== undefined && v !== 0) { out.push(v); break; }"
                                                "    }"
                                                "  }"
                                                "  return out;"
                                                "}"
                                            ),
                                            "returnByValue": True,
                                        })
                                        qo_offsets = (fn_r.get("result") or {}).get("value") or []
                                        log.info(
                                            "CDP tab %s: queryObjects found %d tsOffset(s): %s",
                                            tab_url[:60], len(qo_offsets),
                                            [f"{v:.3f}" for v in qo_offsets],
                                        )
                            except Exception as qo_exc:
                                log.debug("CDP queryObjects failed for %s: %s", tab_url[:60], qo_exc)

                        for vid_info in vid_infos:
                            raw_vct = vid_info.get("currentTime", 0)
                            raw_buf_end = vid_info.get("bufferedEnd")
                            adj_vct = raw_vct - elapsed
                            ts_offset = vid_info.get("timestampOffset")
                            buf_end = (
                                raw_buf_end - elapsed
                                if raw_buf_end is not None
                                else None
                            )
                            # If patch gave us a tsOffset, use it directly.
                            # Otherwise, try each queryObjects offset — the caller's
                            # _cdp_score will reject ones that give implausible latency.
                            if ts_offset is not None:
                                candidates.append((adj_vct, buf_end, ts_offset, tab_url[:80]))
                                log.info(
                                        "CDP tab %s: currentTime=%.3f → adj=%.3f "
                                        "buffered.end=%s tsOffset=%.3f src=%s (patch)",
                                        tab_url[:60], raw_vct, adj_vct,
                                        f"{buf_end:.3f}" if buf_end else "N/A",
                                        ts_offset, vid_info.get("src", ""),
                                )
                            elif qo_offsets:
                                for qo_ts in qo_offsets:
                                    candidates.append((adj_vct, buf_end, qo_ts, tab_url[:80]))
                                    log.info(
                                        "CDP tab %s: currentTime=%.3f → adj=%.3f "
                                        "buffered.end=%s tsOffset=%.3f src=%s (queryObjects)",
                                        tab_url[:60], raw_vct, adj_vct,
                                        f"{buf_end:.3f}" if buf_end else "N/A",
                                        qo_ts, vid_info.get("src", ""),
                                    )
                            else:
                                candidates.append((adj_vct, buf_end, None, tab_url[:80]))
                                log.info(
                                    "CDP tab %s: currentTime=%.3f → adj=%.3f "
                                    "buffered.end=%s tsOffset=N/A src=%s",
                                    tab_url[:60], raw_vct, adj_vct,
                                    f"{buf_end:.3f}" if buf_end else "N/A",
                                    vid_info.get("src", ""),
                                )

            except Exception as exc:
                log.debug("CDP tab %s failed: %s", tab_url[:60], exc)

        return candidates

    async def _capture_video_timing(self, force: bool = False) -> None:
        """Search all frames for a playing <video> and capture timing info.

        Amazon's player uses iframes, so the main-frame JS may not find the
        video. We iterate all Playwright frames to find one with a playing
        video (currentTime > 0).

        Adjusts video.currentTime back to the moment audio_start_unix was
        set, since this method may run seconds later (after retries/sleep).
        """
        import time as _time
        _VIDEO_INFO_JS = """() => {
            // Collect ALL video elements (including in shadow DOM).
            // Return info for the one most buffered (lowest currentTime = furthest behind live).
            function collectVideos(root, out) {
                for (const v of root.querySelectorAll('video')) {
                    if (v.currentTime > 0) out.push(v);
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) collectVideos(el.shadowRoot, out);
                }
            }
            const vids = [];
            collectVideos(document, vids);
            if (!vids.length) return null;
            // Lowest currentTime = most buffered (furthest behind live edge)
            const v = vids.reduce((a, b) => a.currentTime <= b.currentTime ? a : b);
            if (v.currentTime === 0) return null;

            // Strategy 1: init-script monkey-patch stored offsets
            let tsOffset = null;
            try {
                const offsets = window.__mseTimestampOffsets;
                if (offsets) {
                    for (const [mime, val] of Object.entries(offsets)) {
                        if (mime.includes('video') && val !== 0) { tsOffset = val; break; }
                    }
                    if (tsOffset === null) {
                        for (const [mime, val] of Object.entries(offsets)) {
                            if (val !== 0) { tsOffset = val; break; }
                        }
                    }
                }
            } catch(e) {}

            // Strategy 2: walk all MediaSource objects via captured SourceBuffers
            if (tsOffset === null) {
                try {
                    const sbs = window.__capturedSourceBuffers || [];
                    for (const {sb, mimeType} of sbs) {
                        const val = sb.timestampOffset;
                        if (val !== 0 && val !== undefined) {
                            if (mimeType && mimeType.includes('video')) { tsOffset = val; break; }
                            tsOffset = val;
                        }
                    }
                } catch(e) {}
            }

            // Strategy 3: read from video.srcObject (clean W3C path when player
            // passes MediaSource directly) or internal player references
            if (tsOffset === null) {
                try {
                    const ms = v.srcObject || v._mediaSource || v.__mediaSource;
                    if (ms && ms.sourceBuffers) {
                        for (let i = 0; i < ms.sourceBuffers.length; i++) {
                            const sb = ms.sourceBuffers[i];
                            if (sb.timestampOffset !== 0 && sb.timestampOffset !== undefined) {
                                tsOffset = sb.timestampOffset;
                                break;
                            }
                        }
                    }
                } catch(e) {}
            }

            // Strategy 4: read from MediaSource objects captured via
            // URL.createObjectURL interceptor (Amazon uses video.src = blobURL
            // so srcObject is null, but we captured the MediaSource at creation).
            if (tsOffset === null) {
                try {
                    const msList = window.__capturedMediaSources || [];
                    for (const ms of msList) {
                        if (ms.readyState !== 'closed' && ms.sourceBuffers) {
                            for (let i = 0; i < ms.sourceBuffers.length; i++) {
                                const sb = ms.sourceBuffers[i];
                                if (sb.timestampOffset !== 0 && sb.timestampOffset !== undefined) {
                                    tsOffset = sb.timestampOffset;
                                    break;
                                }
                            }
                        }
                        if (tsOffset !== null) break;
                    }
                } catch(e) {}
            }

            return {
                currentTime: v.currentTime,
                bufferedEnd: v.buffered.length > 0
                    ? v.buffered.end(v.buffered.length - 1) : null,
                timestampOffset: tsOffset,
                numVideos: vids.length,
            };
        }"""
        # Collect candidates from all frames, then pick the most-buffered.
        # Amazon's main page has a near-live preview video; the actual player
        # in an iframe is further behind live (lower DASH currentTime = more buffered).
        # We prefer the most-buffered video so the anchor reflects the true playback position.
        candidates: list[tuple[float, float | None, float | None, str]] = []
        # (adj_vct, buffered_end, ts_offset, frame_url)

        # Search ALL pages in the context — Amazon opens the player in a
        # separate tab/popup, not just an iframe of self._page.
        capture_wall = _time.time()
        try:
            all_pages = self._page.context.pages
        except Exception:
            all_pages = [self._page]
        all_frames = [f for pg in all_pages for f in pg.frames]
        log.debug("Searching %d frames across %d pages for video timing", len(all_frames), len(all_pages))
        for frame in all_frames:
            try:
                info = await frame.evaluate(_VIDEO_INFO_JS)
                if info and info.get("currentTime", 0) > 0:
                    raw_vct = info["currentTime"]
                    raw_buf_end = info.get("bufferedEnd")
                    elapsed = capture_wall - (self.audio_start_unix or capture_wall)
                    adj_vct = raw_vct - elapsed
                    adj_buf_end = (
                        raw_buf_end - elapsed
                        if raw_buf_end is not None
                        else None
                    )
                    candidates.append((
                        adj_vct,
                        adj_buf_end,
                        info.get("timestampOffset"),
                        frame.url[:80],
                    ))
                    log.info(
                        "Video candidate in frame %s: currentTime=%.3f → adj=%.3f "
                        "buffered.end=%s tsOffset=%s numVideos=%s",
                        frame.url[:60], raw_vct, adj_vct,
                        f"{adj_buf_end:.3f}" if adj_buf_end else "N/A",
                        f"{info['timestampOffset']:.3f}" if info.get("timestampOffset") else "N/A",
                        info.get("numVideos"),
                    )
            except Exception as exc:
                log.debug("Frame eval error (%s): %s", frame.url[:60], exc)

        if not candidates:
            log.warning("Could not find a playing <video> in any frame")
            return

        # Pick the video with the most buffer ahead (bufferedEnd - currentTime is largest).
        # The live player actively buffers ahead; thumbnail/preview videos have no buffer.
        # If no buffered_end data, fall back to the candidate with a timestampOffset set
        # (that's the player), otherwise take any candidate with currentTime > 0.
        def candidate_score(c):
            adj_vct, buf_end, ts_offset, url = c
            buffer_ahead = (buf_end - adj_vct) if (buf_end is not None and adj_vct > 0) else 0
            has_ts_offset = 1 if ts_offset is not None else 0
            return (has_ts_offset, buffer_ahead)
        best = max(candidates, key=candidate_score)
        best_vct, best_buf_end, best_ts_offset, best_frame = best

        # Only update if this is a genuine improvement (more buffered) or we had no value.
        if (
            force
            or self.video_time_at_start is None
            or self.video_time_at_start == 0
            or best_vct < self.video_time_at_start
        ):
            self.video_time_at_start = best_vct
            self.video_buffered_end = best_buf_end
            # Do not trust frame-level timestampOffset blindly. The validated
            # CDP path below scores vct+tsOffset pairs against AST/wall clock;
            # persisting raw frame values here can lock in preview-player data.
            self._mse_timestamp_offset = None
            self._video_source_frame = best_frame
            self._video_num_candidates = len(candidates)
            log.info(
                "Video timing from frame %s: adj_vct=%.3f buffered.end=%s tsOffset=%s "
                "(%d candidates evaluated)",
                best_frame, best_vct,
                f"{best_buf_end:.3f}" if best_buf_end else "N/A",
                f"{best_ts_offset:.3f}" if best_ts_offset else "N/A",
                len(candidates),
            )
        else:
            self._video_num_candidates = len(candidates)
            self._video_source_frame = best_frame  # record where the best candidate came from
            # Keep the existing timing pair intact. We have already seen cases
            # where a later tsOffset candidate belonged to a different preview
            # player; persisting it here can poison stream_latency with values
            # that were already rejected as implausible elsewhere.
            log.info(
                "Keeping existing vct=%.3f (found %d video candidates, best=%.3f was not more buffered)",
                self.video_time_at_start, len(candidates), best_vct,
            )

    async def _compute_stream_latency(self) -> None:
        """Compute stream latency = audio_start - (AST + playing_DASH_time).

        Uses the MSE timestampOffset (captured via init script monkey-patch)
        to convert video.currentTime to DASH presentation time:
            playing_DASH = video.currentTime - timestampOffset

        Falls back to tfdt-based estimation if the monkey-patch didn't fire.
        """
        from src.parsers.mpd import get_mpd_availability_start

        vct = self.video_time_at_start
        mse_ts_offset = getattr(self, '_mse_timestamp_offset', None)

        # Best path: JS timestampOffset + video.currentTime (no tfdt needed)
        if mse_ts_offset is not None and vct is not None and vct > 0:
            # Need AST from MPD
            ast = None
            if self._amazon_mpd_url:
                try:
                    import httpx
                    headers = dict(self._amazon_headers) if self._amazon_headers else {}
                    headers.pop("host", None)
                    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                        mpd_resp = await client.get(self._amazon_mpd_url, timeout=8.0)
                        ast = get_mpd_availability_start(mpd_resp.content)
                except Exception as exc:
                    log.warning("Failed to fetch MPD for AST: %s", exc)
                if ast is None and self._amazon_mpd_url:
                    log.warning("AST not found in MPD — cannot use mse_timestamp_offset path")

            playing_dash_time = vct - mse_ts_offset
            audio_start = self.audio_start_unix or 0
            log.debug(
                "MSE path: vct=%.3f tsOffset=%.3f playing_dash=%.3f audio_start=%.3f",
                vct, mse_ts_offset, playing_dash_time, audio_start,
            )

            # Formula A: AST-relative decode times (most Amazon streams)
            #   wall = AST + (vct - tsOffset)
            if ast is not None:
                latency_a = audio_start - (ast + playing_dash_time)
                if -5 < latency_a < 60:
                    self.stream_latency = latency_a
                    self.stream_latency_method = "mse_timestamp_offset"
                    log.info(
                        "Stream latency (MSE formula-A): %.2fs "
                        "(tsOffset=%.3f, vct=%.3f, AST=%.3f, wall=%.3f)",
                        latency_a, mse_ts_offset, vct, ast, ast + playing_dash_time,
                    )
                    return
                log.debug("Formula-A latency %.2fs outside -5..60 — trying formula-B", latency_a)

            # Formula B: unix-absolute decode times (some Amazon CDN paths)
            #   wall = vct - tsOffset  (no AST — decode_time IS the unix timestamp)
            latency_b = audio_start - playing_dash_time
            if -5 < latency_b < 60:
                self.stream_latency = latency_b
                self.stream_latency_method = "mse_timestamp_offset"
                log.info(
                    "Stream latency (MSE formula-B, unix decode times): %.2fs "
                    "(tsOffset=%.3f, vct=%.3f, wall=%.3f)",
                    latency_b, mse_ts_offset, vct, playing_dash_time,
                )
                return
            log.debug("Formula-B latency %.2fs outside -5..60 — falling back to tfdt", latency_b)

            log.warning(
                "MSE timestampOffset gave implausible latency (A=%.2fs, B=%.2fs) "
                "for tsOffset=%.3f, vct=%.3f — falling back to tfdt",
                audio_start - (ast + playing_dash_time) if ast else float("nan"),
                latency_b, mse_ts_offset, vct,
            )

        # Fallback: fetch a video segment and parse its tfdt
        if not self._video_segment_requests or not self._amazon_mpd_url:
            log.warning("No video segment requests or MPD URL — cannot compute stream latency")
            return

        import httpx
        from src.parsers.isobmff import extract_tfdt

        # Pick the video segment requested closest to (but before) audio_start
        target_time = self.audio_start_unix or 0
        best = None
        for wall_clock, url in reversed(self._video_segment_requests):
            if wall_clock <= target_time:
                best = (wall_clock, url)
                break
        if best is None and self._video_segment_requests:
            best = self._video_segment_requests[-1]
        if best is None:
            return

        request_wall, seg_url = best
        log.info("Fetching video segment for tfdt: %s (requested at %.3f)", seg_url[-80:], request_wall)

        try:
            headers = dict(self._amazon_headers) if self._amazon_headers else {}
            headers.pop("host", None)
            async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                mpd_resp = await client.get(self._amazon_mpd_url, timeout=8.0)
                ast = get_mpd_availability_start(mpd_resp.content)
                if ast is None:
                    log.warning("No AST in MPD — cannot compute stream latency")
                    return

                seg_resp = await client.get(seg_url, timeout=8.0)
                if seg_resp.status_code != 200:
                    log.warning("Video segment fetch HTTP %d", seg_resp.status_code)
                    return

            result = extract_tfdt(seg_resp.content)
            if result is None:
                log.warning("No tfdt found in video segment")
                return

            base_decode_time, timescale = result
            if timescale == 0:
                timescale = 30000
            segment_dash_time = base_decode_time / timescale

            buf_end = self.video_buffered_end

            if vct is not None and vct > 0 and buf_end is not None and buf_end > 0:
                # Estimate timestampOffset from tfdt + buffer position.
                # Account for time between segment request and buffer capture.
                delta = (self.audio_start_unix or request_wall) - request_wall
                estimated_ts_offset = (segment_dash_time + delta) - buf_end
                playing_dash_time = estimated_ts_offset + vct
                playing_wall_clock = ast + playing_dash_time
                self.stream_latency = (self.audio_start_unix or request_wall) - playing_wall_clock
                self.stream_latency_method = "tfdt_plus_buffer"
                log.info(
                    "Stream latency from tfdt+buffer: %.2fs "
                    "(seg_dash=%.3f, delta=%.1fs, buf_end=%.3f, "
                    "est_tsOffset=%.3f, vct=%.3f, playing_dash=%.3f, "
                    "playing_wall=%.3f, audio_start=%.3f)",
                    self.stream_latency,
                    segment_dash_time,
                    delta,
                    buf_end,
                    estimated_ts_offset,
                    vct,
                    playing_dash_time,
                    playing_wall_clock,
                    self.audio_start_unix or 0,
                )
            else:
                # Last resort: CDN latency only
                segment_wall_clock = ast + segment_dash_time
                self.stream_latency = request_wall - segment_wall_clock
                self.stream_latency_method = "tfdt_cdn_only"
                log.info(
                    "Stream latency from tfdt (CDN only, no buffer data): %.2fs "
                    "(request_wall=%.3f, segment_wall=%.3f)",
                    self.stream_latency,
                    request_wall,
                    segment_wall_clock,
                )
        except Exception as exc:
            log.warning("Failed to compute stream latency from tfdt: %s", exc)

    async def _save_dash_audio_reference(self, output_dir: Path) -> None:
        """Download DASH audio segments for post-session stream latency cross-correlation.

        Saves all segments available in the MPD (typically ~60-120s coverage) as
        individual .m4s files named '{dash_time:.3f}_{segment_id}.m4s'.  evaluate.py
        cross-correlates these against the recorded audio.wav to determine exact
        stream latency without relying on MSE internals.
        """
        if not self._amazon_mpd_url:
            return

        from src.parsers.mpd import parse_mpd_audio_segments, get_mpd_availability_start
        import json as _json

        try:
            import httpx
            import asyncio as _aio
            headers = dict(self._amazon_headers) if self._amazon_headers else {}
            headers.pop("host", None)
            async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                mpd_resp = await client.get(self._amazon_mpd_url, timeout=8.0)
                if mpd_resp.status_code != 200:
                    return

                ast = get_mpd_availability_start(mpd_resp.content)
                segments = parse_mpd_audio_segments(mpd_resp.content, self._amazon_mpd_url)
                if not segments:
                    log.warning("No audio segments found in MPD")
                    return
                encrypted = any(
                    "cenc_" in seg.init_url.lower() or "cenc_" in seg.url.lower()
                    for seg in segments
                )

                audio_dir = output_dir / "dash_audio"
                audio_dir.mkdir(parents=True, exist_ok=True)

                init_by_group = {seg.group_id: seg.init_url for seg in segments}
                first_init_saved = False
                for group_id, init_url in init_by_group.items():
                    init_resp = await client.get(init_url, timeout=8.0)
                    if init_resp.status_code != 200:
                        log.warning("Audio init segment HTTP %d for %s", init_resp.status_code, group_id)
                        continue
                    init_path = audio_dir / f"init_{group_id}.mp4"
                    init_path.write_bytes(init_resp.content)
                    if not first_init_saved:
                        (audio_dir / "init.mp4").write_bytes(init_resp.content)
                        first_init_saved = True

                # Download all media segments concurrently
                async def _fetch_seg(seg):
                    fname = f"{seg.dash_time:.3f}_{seg.group_id}_{seg.segment_id}.m4s"
                    seg_path = audio_dir / fname
                    if seg_path.exists():
                        return True
                    try:
                        resp = await client.get(seg.url, timeout=8.0)
                        if resp.status_code == 200:
                            seg_path.write_bytes(resp.content)
                            return True
                        log.debug("Audio seg HTTP %d: %s", resp.status_code, seg.url[-60:])
                    except Exception as exc:
                        log.debug("Audio seg fetch failed: %s", exc)
                    return False

                results = await _aio.gather(*[_fetch_seg(s) for s in segments])
                saved = sum(1 for r in results if r)

                # Save metadata so evaluate.py knows the absolute timeline anchor
                coverage = segments[-1].dash_time - segments[0].dash_time if len(segments) > 1 else 0
                (audio_dir / "meta.json").write_text(_json.dumps({
                    "ast": ast,
                    "first_dash_time": segments[0].dash_time,
                    "last_dash_time": segments[-1].dash_time,
                    "num_segments": len(segments),
                    "encrypted": encrypted,
                }, indent=2))

                log.info(
                    "Saved %d/%d DASH audio segments (%.1fs coverage, first_dash_time=%.3f, encrypted=%s)",
                    saved, len(segments), coverage, segments[0].dash_time, encrypted,
                )
        except Exception as exc:
            log.warning("Failed to save DASH audio segments: %s", exc)

    async def stop_and_save(self) -> Path | None:
        """Stop recording, flush remaining chunks, convert to WAV via ffmpeg."""
        if not self._recording:
            log.warning("Audio recording was never started")
            return None

        self._recording = False

        if self._timing_task and not self._timing_task.done():
            self._timing_task.cancel()
            try:
                await self._timing_task
            except asyncio.CancelledError:
                pass
        if self._best_timing_state is not None:
            self._restore_timing_state(self._best_timing_state)

        # Cancel the periodic flush task
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        try:
            # Collect calibration beep offset
            try:
                beep_offset = await self._page.evaluate(
                    "() => window.__calibrationBeepOffset"
                )
                if beep_offset is not None:
                    self.calibration_beep_offset = beep_offset
                    log.info("Calibration beep was injected at %.3fs from recording start", beep_offset)
            except Exception:
                pass

            # Stop the MediaRecorder
            try:
                await self._page.evaluate(_RECORDER_STOP_JS)
                log.info("Audio recording stopped, collecting remaining data...")
                # Brief wait for final ondataavailable + FileReader callbacks
                await asyncio.sleep(1)
                # Drain any final chunks
                await self._drain_and_write()
            except Exception as exc:
                log.warning("Could not stop recorder via JS (browser may have disconnected): %s", exc)

        except Exception as exc:
            log.warning("Error during recorder stop: %s", exc)

        # Convert accumulated WebM to WAV
        if not self._webm_path or not self._webm_path.exists():
            log.warning("No audio data on disk")
            return None

        size_mb = self._webm_path.stat().st_size / 1024 / 1024
        log.info("Total audio data: %.1f MB", size_mb)

        if size_mb < 0.001:
            log.warning("Audio file is empty")
            self._webm_path.unlink(missing_ok=True)
            return None

        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(self._webm_path),
             "-ar", "16000", "-ac", "1",
             str(self._wav_path)],
            capture_output=True, text=True,
        )
        self._webm_path.unlink(missing_ok=True)

        if result.returncode != 0:
            log.error("ffmpeg conversion failed: %s", result.stderr[-500:])
            return None

        size_mb = self._wav_path.stat().st_size / 1024 / 1024
        log.info("Audio saved: %s (%.1f MB)", self._wav_path, size_mb)
        return self._wav_path

    async def close(self) -> None:
        """Close browser and terminate Chrome process."""
        timing_task = self._timing_task
        if timing_task and not timing_task.done():
            timing_task.cancel()
            try:
                await timing_task
            except asyncio.CancelledError:
                pass
        # Cancel the CDP target interceptor task if present
        cdp_task = getattr(self, '_cdp_interceptor_task', None)
        if cdp_task and not cdp_task.done():
            cdp_task.cancel()
            try:
                await cdp_task
            except asyncio.CancelledError:
                pass
        # Close all pages first (graceful shutdown triggers beforeunload etc.)
        try:
            if self._pw_context:
                ctx = self._browser.contexts[0] if (self._browser and self._browser.contexts) else None
                if ctx:
                    for pg in list(ctx.pages):
                        try:
                            await pg.close()
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        if self._chrome_proc:
            self._chrome_proc.terminate()
            try:
                self._chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._chrome_proc.kill()
        # Disconnect Playwright
        try:
            if self._pw_context:
                await self._pw_context.stop()
        except Exception:
            pass


# Persistent browser profile directories — keeps cookies, login state, saved passwords
# Separate profiles per broadcaster to avoid cookie conflicts
BROWSER_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile"
ITV_BROWSER_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile_itv"

# Chromium args to reduce bot detection fingerprinting
_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
]


async def capture_session(
    channel: str,
    timeout_s: int = 60,
    headless: bool = False,
    record_audio: bool = False,
    live_url: str | None = None,
) -> tuple[CapturedSession, BrowserAudioRecorder | None]:
    """Launch iPlayer in Chromium, wait for a subtitle request, extract session params.

    Uses a persistent browser profile so BBC login state is preserved between runs.
    On first use, sign in to BBC — subsequent runs will skip the login step.

    If record_audio=True, the browser stays open and returns a
    BrowserAudioRecorder. The caller must call recorder.stop_and_save()
    and recorder.close() when done.
    """
    iplayer_url = live_url or BBC_IPLAYER_LIVE_URLS.get(channel)
    if not iplayer_url:
        raise ValueError(
            f"No iPlayer URL mapped for channel {channel!r}. "
            f"Valid: {', '.join(BBC_IPLAYER_LIVE_URLS)}"
        )

    subtitle_info: dict | None = None
    video_repr: str | None = None
    video_base_url: str | None = None
    captured_event = asyncio.Event()

    def on_request(request: Request) -> None:
        nonlocal subtitle_info, video_repr, video_base_url

        url = request.url

        if video_repr is None:
            vm = _VIDEO_URL_RE.search(url)
            if vm:
                video_repr = vm.group("video_repr")
                video_base_url = vm.group("base")
                log.info("Captured video: base=%s repr=%s", video_base_url, video_repr)

        if subtitle_info is None:
            m = _SUBTITLE_URL_RE.search(url)
            if m:
                subtitle_info = {
                    "base_url": m.group("base"),
                    "x_param": int(m.group("x")),
                    "channel": m.group("channel"),
                    "headers": dict(request.headers),
                }
                log.info(
                    "Captured subtitle: x=%d channel=%s",
                    subtitle_info["x_param"], subtitle_info["channel"],
                )
                captured_event.set()

    first_run = not BROWSER_PROFILE_DIR.exists()

    pw = await async_playwright().start()
    browser_args = list(_CHROMIUM_ARGS)
    if record_audio:
        browser_args.extend(_AUDIO_CAPTURE_CHROME_ARGS)
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 720},
        args=browser_args,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    page.on("request", on_request)

    log.info("Opening iPlayer: %s", iplayer_url)
    if first_run:
        print(f"\nOpening iPlayer in Chromium (first run — sign in to save session)...")
        print(f"  1. Dismiss any cookie banners")
        print(f"  2. Sign in to your BBC account")
        print(f"  3. Make sure subtitles are ON (S key or CC button)")
    else:
        print(f"\nOpening iPlayer in Chromium (using saved session)...")
        print(f"  Make sure subtitles are ON if not already enabled")
    print(f"  Waiting up to {timeout_s}s for subtitle traffic...\n")

    await page.goto(iplayer_url, wait_until="domcontentloaded")

    try:
        await asyncio.wait_for(captured_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await context.close()
        await pw.stop()
        raise TimeoutError(
            f"No subtitle requests captured within {timeout_s}s. "
            "Make sure the stream is playing and subtitles are enabled."
        )

    # Give a few extra seconds for video URL to arrive
    if video_repr is None:
        log.info("Waiting up to 5s for video URL...")
        for _ in range(10):
            await asyncio.sleep(0.5)
            if video_repr is not None:
                break

    if subtitle_info is None:
        await context.close()
        await pw.stop()
        raise RuntimeError("subtitle_info is None after capture")

    result = CapturedSession(
        base_url=subtitle_info["base_url"],
        x_param=subtitle_info["x_param"],
        channel=subtitle_info["channel"],
        headers=subtitle_info["headers"],
        video_repr=video_repr,
        video_base_url=video_base_url,
    )
    log.info(
        "Session: sub_cdn=%s video_cdn=%s x=%d channel=%s video=%s",
        result.base_url, result.video_base_url, result.x_param,
        result.channel, result.video_repr,
    )

    recorder = None
    if record_audio:
        # BBC uses Playwright's Chromium (no external Chrome process)
        recorder = BrowserAudioRecorder(page, chrome_proc=None, browser=context, pw_context=pw)
    else:
        await context.close()
        await pw.stop()

    return result, recorder


def _find_chrome() -> str | None:
    """Find the system Chrome/Chromium executable path."""
    import platform
    import shutil

    system = platform.system()
    if system == "Darwin":
        # macOS: check standard Chrome install location
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.exists():
            return str(chrome)
    elif system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            path = shutil.which(name)
            if path:
                return path
    elif system == "Windows":
        for prog in (Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
                      Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                      Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")):
            if prog.exists():
                return str(prog)
    return None


# --- ITV (ITVX) ---

# Matches ITV MPD manifest URLs
_ITV_MPD_RE = re.compile(r"https?://[^\"'\s]+\.mpd")

ITVX_LIVE_URLS = {
    "itv1": "https://www.itv.com/watch?channel=itv",
    "itv2": "https://www.itv.com/watch?channel=itv2",
    "itv3": "https://www.itv.com/watch?channel=itv3",
    "itv4": "https://www.itv.com/watch?channel=itv4",
    "itvbe": "https://www.itv.com/watch?channel=itvbe",
    "citv": "https://www.itv.com/watch?channel=citv",
    "fast18": "https://www.itv.com/watch?channel=fast18",
}


async def capture_itv_session(
    channel: str,
    timeout_s: int = 90,
    headless: bool = False,
    record_audio: bool = False,
) -> tuple[CapturedITVSession, BrowserAudioRecorder | None]:
    """Capture ITVX MPD URL by launching Chrome with remote debugging.

    ITVX requires Widevine DRM, which Playwright's Chromium lacks. Instead,
    we launch the user's real Chrome with --remote-debugging-port and connect
    to it via CDP. This gives us network interception on a real Chrome that
    has full DRM support.

    If record_audio=True, the browser stays open and returns a
    BrowserAudioRecorder that captures tab audio. The caller must
    call recorder.stop_and_save() and recorder.close() when done.
    """
    itvx_url = ITVX_LIVE_URLS.get(channel)
    if not itvx_url:
        raise ValueError(
            f"No ITVX URL mapped for channel {channel!r}. "
            f"Valid: {', '.join(ITVX_LIVE_URLS)}"
        )

    chrome_path = _find_chrome()
    if not chrome_path:
        raise RuntimeError(
            "System Chrome not found. Install Google Chrome or use --mpd-url instead."
        )

    debug_port = 9222
    profile_dir = str(ITV_BROWSER_PROFILE_DIR)

    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    if record_audio:
        chrome_args.extend(_AUDIO_CAPTURE_CHROME_ARGS)
    if headless:
        chrome_args.append("--headless=new")

    log.info("Launching Chrome with remote debugging on port %d", debug_port)
    chrome_proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    await asyncio.sleep(2)

    mpd_info: dict | None = None
    captured_event = asyncio.Event()

    def on_request(request: Request) -> None:
        nonlocal mpd_info
        if mpd_info is not None:
            return
        url = request.url
        if ".mpd" in url:
            mpd_info = {
                "mpd_url": url,
                "headers": dict(request.headers),
            }
            log.info("Captured MPD URL: %s", url[:120])
            captured_event.set()

    first_run = not ITV_BROWSER_PROFILE_DIR.exists()

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(
        f"http://localhost:{debug_port}",
    )
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    page.on("request", on_request)

    if first_run:
        print(f"\nOpening ITVX in Chrome (first run — sign in to save session)...")
        print(f"  1. Dismiss any cookie banners")
        print(f"  2. Sign in to your ITVX account")
        print(f"  3. Wait for the stream to start playing")
    else:
        print(f"\nOpening ITVX in Chrome (using saved session)...")
        print(f"  Wait for the stream to start playing")
    print(f"  Waiting up to {timeout_s}s for MPD manifest traffic...\n")

    await page.goto(itvx_url, wait_until="domcontentloaded")

    try:
        await asyncio.wait_for(captured_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()
        raise TimeoutError(
            f"No MPD manifest captured within {timeout_s}s. "
            "Make sure the stream is playing with subtitles enabled. "
            "If you see a 10-01 error, use --mpd-url instead."
        )

    if mpd_info is None:
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()
        raise RuntimeError("mpd_info is None after capture")

    result = CapturedITVSession(
        mpd_url=mpd_info["mpd_url"],
        headers=mpd_info["headers"],
    )
    log.info("ITV session: mpd_url=%s", result.mpd_url[:120])

    recorder = None
    if record_audio:
        recorder = BrowserAudioRecorder(page, chrome_proc, browser, pw)
    else:
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()

    return result, recorder


# --- Channel 4 ---

C4_BROWSER_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile_c4"

C4_LIVE_URLS = {
    "channel4": "https://www.channel4.com/now/C4",
    "e4": "https://www.channel4.com/now/E4",
    "more4": "https://www.channel4.com/now/M4",
    "film4": "https://www.channel4.com/now/F4",
    "4seven": "https://www.channel4.com/now/4S",
}


async def capture_c4_session(
    channel: str,
    timeout_s: int = 90,
    headless: bool = False,
    record_audio: bool = False,
    live_url: str | None = None,
) -> tuple[CapturedITVSession, BrowserAudioRecorder | None]:
    """Capture Channel 4 MPD URL by launching Chrome with remote debugging.

    Same CDP approach as ITV — Channel 4 also requires Widevine DRM.
    Returns a CapturedITVSession (same shape: mpd_url + headers).

    If record_audio=True, the browser stays open and returns a
    BrowserAudioRecorder. The caller must call recorder.stop_and_save()
    and recorder.close() when done.
    """
    c4_url = live_url or C4_LIVE_URLS.get(channel)
    if not c4_url:
        raise ValueError(
            f"No Channel 4 URL mapped for channel {channel!r}. "
            f"Valid: {', '.join(C4_LIVE_URLS)}"
        )

    chrome_path = _find_chrome()
    if not chrome_path:
        raise RuntimeError(
            "System Chrome not found. Install Google Chrome or use --mpd-url instead."
        )

    debug_port = 9223
    profile_dir = str(C4_BROWSER_PROFILE_DIR)

    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=ChromeWhatsNewUI",
        "--disable-infobars",
    ]
    if record_audio:
        chrome_args.extend(_AUDIO_CAPTURE_CHROME_ARGS)
    if headless:
        chrome_args.append("--headless=new")

    log.info("Launching Chrome with remote debugging on port %d", debug_port)
    chrome_proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    await asyncio.sleep(2)

    mpd_info: dict | None = None
    subtitle_url: str | None = None
    captured_event = asyncio.Event()

    def on_request(request: Request) -> None:
        nonlocal mpd_info, subtitle_url
        url = request.url
        # Log any manifest-like traffic for diagnostics
        if ".m3u8" in url:
            log.info("C4 HLS manifest seen (ignored): %s", url[:160])
        if ".mpd" in url and mpd_info is None:
            log.info("C4 MPD request seen: %s", url[:160])
            mpd_info = {
                "mpd_url": url,
                "headers": dict(request.headers),
            }
            log.info("Captured C4 content MPD: %s", url[:120])
            captured_event.set()
        # Capture sidecar subtitle file (VOD streams deliver subtitles separately)
        if subtitle_url is None and (".vtt" in url.lower() or ".ttml" in url.lower() or ".dfxp" in url.lower()):
            log.info("C4 subtitle sidecar URL captured: %s", url[:160])
            subtitle_url = url

    first_run = not C4_BROWSER_PROFILE_DIR.exists()

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(
        f"http://localhost:{debug_port}",
    )
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Mask automation signals before any page JS runs so C4's adblock detector
    # doesn't trip on navigator.webdriver or chrome.runtime fingerprints.
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        if (window.chrome && window.chrome.runtime) {
            Object.defineProperty(window.chrome.runtime, 'connect', { get: () => undefined });
        }
    """)

    # Listen on ALL existing pages (the player may load in an existing tab)
    for existing_page in context.pages:
        existing_page.on("request", on_request)

    # Also listen on any new pages that open
    def on_new_page(new_page: Page) -> None:
        new_page.on("request", on_request)
    context.on("page", on_new_page)

    page = context.pages[0] if context.pages else await context.new_page()
    page.on("request", on_request)

    if first_run:
        print(f"\nOpening Channel 4 in Chrome (first run — sign in to save session)...")
        print(f"  1. Dismiss any cookie banners")
        print(f"  2. Sign in to your Channel 4 account")
        print(f"  3. Wait for the stream to start playing")
    else:
        print(f"\nOpening Channel 4 in Chrome (using saved session)...")
        print(f"  Wait for the stream to start playing")
    print(f"  Waiting up to {timeout_s}s for MPD manifest traffic...\n")

    await page.goto(c4_url, wait_until="domcontentloaded")

    try:
        await asyncio.wait_for(captured_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()
        raise TimeoutError(
            f"No MPD manifest captured within {timeout_s}s. "
            "Make sure the stream is playing. "
            "If blocked, use --mpd-url instead."
        )

    if mpd_info is None:
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()
        raise RuntimeError("mpd_info is None after capture")

    # Wait briefly for sidecar subtitle URL if not already seen
    if subtitle_url is None:
        log.info("Waiting up to 15s for subtitle sidecar URL...")
        await asyncio.sleep(15)

    result = CapturedITVSession(
        mpd_url=mpd_info["mpd_url"],
        headers=mpd_info["headers"],
        subtitle_url=subtitle_url,
    )
    log.info("C4 session: mpd_url=%s subtitle_url=%s", result.mpd_url[:120], result.subtitle_url)

    recorder = None
    if record_audio:
        recorder = BrowserAudioRecorder(page, chrome_proc, browser, pw)
    else:
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()

    return result, recorder


# --- Amazon Prime Video ---

AMAZON_BROWSER_PROFILE_DIR = Path(__file__).resolve().parent.parent / ".browser_profile_amazon"

AMAZON_LIVE_URLS = {
    "amazon_cnn": "https://www.amazon.co.uk/gp/video/livetv",
    "amazon_premier_league": "https://www.amazon.co.uk/gp/video/livetv",
    "amazon_nfl": "https://www.amazon.co.uk/gp/video/livetv",
    "amazon_champions_league": "https://www.amazon.co.uk/gp/video/livetv",
}

_AMAZON_MANIFEST_URL_RE = re.compile(r"https?://[^\"'\s<>]+", re.IGNORECASE)
_AMAZON_CONFIG_HINTS = (
    "/playback",
    "playbackresources",
    "getplayback",
    "/manifest",
    "dash",
    "subtitle",
    "caption",
)
_AMAZON_HEADER_SKIP = {"host", "connection", "content-length", "transfer-encoding"}


def _looks_like_amazon_manifest_url(url: str) -> bool:
    lower = url.lower()
    return ".mpd" in lower or ("/manifest" in lower and "dash" in lower)


def _looks_like_amazon_config_url(url: str) -> bool:
    lower = url.lower()
    if _looks_like_amazon_manifest_url(url):
        return True
    return any(hint in lower for hint in _AMAZON_CONFIG_HINTS)


def _extract_amazon_manifest_urls(text: str) -> list[str]:
    """Extract embedded manifest URLs from JSON/text payloads."""
    results: list[str] = []
    seen: set[str] = set()

    for blob in (text, text.replace("\\/", "/")):
        for match in _AMAZON_MANIFEST_URL_RE.findall(blob):
            url = match.rstrip('",}])>\\')
            if not _looks_like_amazon_manifest_url(url) or url in seen:
                continue
            seen.add(url)
            results.append(url)

    return results


def _inspect_amazon_mpd_candidate(mpd_bytes: bytes, mpd_url: str) -> tuple[bool, str]:
    """Return whether an MPD looks like the live subtitle manifest."""
    import time as _time

    from src.parsers.mpd import (
        get_mpd_availability_start,
        get_mpd_type,
        get_mpd_utc_timing,
        parse_mpd_subtitle_segments,
    )

    mpd_type = get_mpd_type(mpd_bytes) or "unknown"
    ast = get_mpd_availability_start(mpd_bytes)
    utc_hint = get_mpd_utc_timing(mpd_bytes)
    segments = parse_mpd_subtitle_segments(mpd_bytes, mpd_url)

    reasons: list[str] = []
    if mpd_type != "dynamic":
        reasons.append(f"type={mpd_type}")
    if ast is None:
        reasons.append("no availabilityStartTime")
    if not segments:
        reasons.append("no subtitle segments")

    drift_s: float | None = None
    if ast is not None and segments:
        drift_s = abs(_time.time() - (ast + segments[-1].wall_clock))
        if drift_s > 900:
            reasons.append(f"subtitle edge drift {drift_s:.0f}s")
    elif utc_hint is not None:
        drift_s = abs(_time.time() - utc_hint)
        if drift_s > 900:
            reasons.append(f"utc hint drift {drift_s:.0f}s")

    summary = (
        f"type={mpd_type}, ast={'yes' if ast is not None else 'no'}, "
        f"subs={len(segments)}, drift={'n/a' if drift_s is None else f'{drift_s:.0f}s'}"
    )
    if reasons:
        return False, f"{summary} | {'; '.join(reasons)}"
    return True, summary


async def capture_amazon_session(
    channel: str,
    timeout_s: int = 90,
    headless: bool = False,
    record_audio: bool = False,
    live_url: str | None = None,
    wait_for_enter: bool = False,
) -> tuple[CapturedAmazonSession, BrowserAudioRecorder | None]:
    """Capture Amazon Prime Video MPD URL by launching Chrome with remote debugging.

    Amazon requires Widevine DRM for video/audio, so we use system Chrome.
    Subtitles are unencrypted TTML in MP4 despite the cenc- filename prefix.

    If record_audio=True, the browser stays open and returns a
    BrowserAudioRecorder. The caller must call recorder.stop_and_save()
    and recorder.close() when done.
    """
    amazon_url = live_url or AMAZON_LIVE_URLS.get(channel)
    if not amazon_url:
        raise ValueError(
            f"No Amazon URL mapped for channel {channel!r}. "
            f"Valid: {', '.join(AMAZON_LIVE_URLS)}. "
            f"Or pass --live-url to override."
        )

    chrome_path = _find_chrome()
    if not chrome_path:
        raise RuntimeError(
            "System Chrome not found. Install Google Chrome or use --mpd-url instead."
        )

    debug_port = 9224
    profile_dir = str(AMAZON_BROWSER_PROFILE_DIR)

    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        # Disable cross-origin iframe process isolation so Playwright's
        # frame.evaluate() can reach the Amazon player iframe (which loads
        # under a different origin, e.g. atv-ps.amazon.co.uk). Without this,
        # Chrome puts cross-origin iframes in separate renderer processes and
        # JS evaluation via CDP is blocked by site isolation.
        "--disable-site-isolation-trials",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    if record_audio:
        chrome_args.extend(_AUDIO_CAPTURE_CHROME_ARGS)
    if headless:
        chrome_args.append("--headless=new")

    log.info("Launching Chrome with remote debugging on port %d", debug_port)
    chrome_proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    await asyncio.sleep(2)

    mpd_info: dict | None = None
    captured_event = asyncio.Event()
    armed = False  # only start matching MPD requests after user confirms target stream
    candidate_urls_seen: set[str] = set()
    candidate_queue: asyncio.Queue[tuple[str, dict[str, str], str]] = asyncio.Queue()
    candidate_rejections: list[str] = []
    # Track video segment requests with wall-clock timestamps for tfdt-based timing
    import time as _time
    video_segment_requests: list[tuple[float, str]] = []  # (wall_clock, url)

    def queue_candidate(url: str, headers: dict[str, str], source: str) -> None:
        if not armed or mpd_info is not None or not _looks_like_amazon_config_url(url):
            return
        if url in candidate_urls_seen:
            return
        candidate_urls_seen.add(url)
        candidate_queue.put_nowait((url, dict(headers), source))
        log.debug("Queued Amazon candidate from %s: %s", source, url[:200])

    async def resolve_candidate_url(
        url: str,
        headers: dict[str, str],
        source: str,
        depth: int = 0,
    ) -> tuple[dict[str, str] | None, str]:
        import httpx

        filtered_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in _AMAZON_HEADER_SKIP
        }
        try:
            async with httpx.AsyncClient(
                headers=filtered_headers,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, timeout=8.0)
        except Exception as exc:
            return None, f"{source}: fetch failed for {url[:120]} ({exc})"

        final_url = str(resp.url)
        body = resp.content
        content_type = resp.headers.get("content-type", "").lower()
        if resp.status_code != 200:
            return None, f"{source}: HTTP {resp.status_code} for {final_url[:120]}"

        if body.lstrip().startswith(b"<MPD") or _looks_like_amazon_manifest_url(final_url):
            try:
                ok, summary = _inspect_amazon_mpd_candidate(body, final_url)
            except Exception as exc:
                return None, f"{source}: failed to inspect MPD {final_url[:120]} ({exc})"
            if ok:
                return {
                    "mpd_url": final_url,
                    "headers": filtered_headers,
                }, f"{source}: accepted {summary}"
            return None, f"{source}: rejected {final_url[:120]} ({summary})"

        if depth >= 1:
            return None, f"{source}: no manifest in nested payload {final_url[:120]}"

        if "json" not in content_type and "text" not in content_type:
            return None, f"{source}: non-text payload {content_type or 'unknown'} at {final_url[:120]}"

        embedded_urls = _extract_amazon_manifest_urls(resp.text)
        if not embedded_urls:
            return None, f"{source}: no embedded MPD URL in {final_url[:120]}"

        for embedded_url in embedded_urls:
            result, detail = await resolve_candidate_url(
                embedded_url,
                filtered_headers,
                f"{source} -> embedded",
                depth + 1,
            )
            if result is not None:
                return result, detail

        return None, f"{source}: embedded URLs rejected from {final_url[:120]}"

    async def validate_candidates() -> None:
        nonlocal mpd_info

        while mpd_info is None:
            url, headers, source = await candidate_queue.get()
            try:
                resolved, detail = await resolve_candidate_url(url, headers, source)
                if resolved is not None:
                    mpd_info = resolved
                    log.info("Captured Amazon MPD URL: %s", resolved["mpd_url"][:200])
                    log.debug("Amazon MPD validation: %s", detail)
                    captured_event.set()
                    return
                candidate_rejections.append(detail)
                if len(candidate_rejections) > 6:
                    candidate_rejections.pop(0)
                log.debug("Rejected Amazon candidate: %s", detail)
            finally:
                candidate_queue.task_done()

    validator_task = asyncio.create_task(validate_candidates())

    def on_request(request: Request) -> None:
        nonlocal mpd_info
        url = request.url
        # Track video segment requests (cenc_video_*.mp4?m=...)
        if "cenc_video_" in url and ".mp4" in url:
            video_segment_requests.append((_time.time(), url))
            if len(video_segment_requests) == 1:
                log.debug("First video segment request: %s", url[-100:])
            # Keep only the last 20 to avoid memory growth
            if len(video_segment_requests) > 20:
                video_segment_requests.pop(0)
        if not armed or mpd_info is not None:
            return
        # Log video-related requests for debugging
        resource = request.resource_type
        if resource in ("media", "xhr", "fetch"):
            for hint in (".mpd", "/manifest", "dash", "/subtitles", "cenc-sd"):
                if hint in url.lower():
                    log.debug("Candidate media request: %s", url[:200])
                    break
        if resource in ("media", "xhr", "fetch"):
            queue_candidate(url, dict(request.headers), f"request:{resource}")

    first_run = not AMAZON_BROWSER_PROFILE_DIR.exists()

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(
        f"http://localhost:{debug_port}",
    )
    context = browser.contexts[0] if browser.contexts else await browser.new_context()

    # Listen on ALL existing pages (the live player may load in an existing tab)
    for existing_page in context.pages:
        existing_page.on("request", on_request)

    # Also listen on any new pages that open
    def on_new_page(new_page: Page) -> None:
        new_page.on("request", on_request)
    context.on("page", on_new_page)

    page = context.pages[0] if context.pages else await context.new_page()
    page.on("request", on_request)

    # Use a CDP session to intercept ALL network requests (including cross-origin
    # iframes that Playwright's page.on("request") may miss in CDP-connected mode).
    # This is the authoritative path for video segment request tracking.
    try:
        cdp_session = await context.new_cdp_session(page)
        await cdp_session.send("Network.enable")

        def on_cdp_request(params: dict) -> None:
            nonlocal mpd_info
            url = params.get("request", {}).get("url", "")
            if "cenc_video_" in url and ".mp4" in url:
                video_segment_requests.append((_time.time(), url))
                if len(video_segment_requests) == 1:
                    log.debug("CDP: first video segment request: %s", url[-100:])
                if len(video_segment_requests) > 20:
                    video_segment_requests.pop(0)
            if not armed or mpd_info is not None:
                return
            req_headers = params.get("request", {}).get("headers", {})
            queue_candidate(url, req_headers, "cdp")

        cdp_session.on("Network.requestWillBeSent", on_cdp_request)
        log.debug("CDP Network listener attached")
    except Exception as exc:
        log.debug("CDP Network listener setup failed: %s", exc)

    # MSE patch script — intercepts addSourceBuffer and URL.createObjectURL.
    # Also stores blobUrl→MediaSource mapping so we can match tsOffset to
    # the correct video element when reading back later.
    _MSE_PATCH_INLINE = """
        (() => {
            if (window.__msePatchApplied) return;
            window.__msePatchApplied = true;
            if (!window.__msBlobUrls) window.__msBlobUrls = {};
            if (!window.__capturedMediaSources) window.__capturedMediaSources = [];
            let __msIdCounter = 0;
            const origCreateObjectURL = URL.createObjectURL.bind(URL);
            URL.createObjectURL = function(obj) {
                const url = origCreateObjectURL(obj);
                if (obj instanceof MediaSource) {
                    const id = ++__msIdCounter;
                    obj.__id__ = id;
                    window.__msBlobUrls[id] = url;
                    window.__capturedMediaSources.push(obj);
                }
                return url;
            };
            const origAddSB = MediaSource.prototype.addSourceBuffer;
            MediaSource.prototype.addSourceBuffer = function(mimeType) {
                const sb = origAddSB.call(this, mimeType);
                if (!window.__capturedSourceBuffers) window.__capturedSourceBuffers = [];
                window.__capturedSourceBuffers.push({sb, mimeType});
                const proto = sb.__proto__.__proto__;
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'timestampOffset')
                    || Object.getOwnPropertyDescriptor(sb.__proto__, 'timestampOffset');
                if (descriptor && descriptor.set) {
                    const origSet = descriptor.set;
                    const origGet = descriptor.get;
                    Object.defineProperty(sb, 'timestampOffset', {
                        get() { return origGet.call(this); },
                        set(val) {
                            origSet.call(this, val);
                            if (!window.__mseTimestampOffsets) window.__mseTimestampOffsets = {};
                            window.__mseTimestampOffsets[mimeType] = val;
                        },
                        configurable: true,
                    });
                }
                return sb;
            };
        })();
    """

    # --- Raw CDP WebSocket interceptor ---
    # Amazon opens the CNN player in a SECOND Chrome tab that Playwright does
    # NOT see via context.pages (it's opened externally by the browser UI, not
    # via window.open intercepted by Playwright).
    #
    # We open a raw WebSocket to the browser-level CDP endpoint and use
    # Target.setAutoAttach + waitForDebuggerOnStart=true.  Chrome will pause
    # every NEW page target before running any JS, letting us inject
    # Page.addScriptToEvaluateOnNewDocument BEFORE the Amazon player can call
    # addSourceBuffer — so our MSE monkey-patch always fires first.
    import aiohttp as _aiohttp
    import json as _json

    _cdp_interceptor_task: asyncio.Task | None = None

    async def _run_cdp_target_interceptor():
        try:
            async with _aiohttp.ClientSession() as _http:
                _r = await _http.get(f"http://localhost:{debug_port}/json/version")
                _info = await _r.json(content_type=None)
                _ws_url = _info["webSocketDebuggerUrl"]
            log.debug("CDP interceptor: connecting to %s", _ws_url)

            _msg_counter = 0

            async with _aiohttp.ClientSession() as _ws_session:
                async with _ws_session.ws_connect(_ws_url, heartbeat=30) as _ws:

                    async def _cdp_send(method, params=None, session_id=None):
                        nonlocal _msg_counter
                        _msg_counter += 1
                        _msg = {"id": _msg_counter, "method": method, "params": params or {}}
                        if session_id:
                            _msg["sessionId"] = session_id
                        await _ws.send_str(_json.dumps(_msg))

                    # Attach to all existing and future targets; pause new ones
                    # before their JS runs so we can inject our init script first.
                    await _cdp_send("Target.setAutoAttach", {
                        "autoAttach": True,
                        "waitForDebuggerOnStart": True,
                        "flatten": True,
                    })
                    log.debug("CDP interceptor: Target.setAutoAttach enabled")

                    async for _cdp_msg in _ws:
                        if _cdp_msg.type != _aiohttp.WSMsgType.TEXT:
                            break
                        _data = _json.loads(_cdp_msg.data)
                        _method = _data.get("method", "")
                        # Capture video segment requests from any session
                        if _method == "Network.requestWillBeSent":
                            _request = _data.get("params", {}).get("request", {})
                            _req_url = _request.get("url", "")
                            if "cenc_video_" in _req_url and ".mp4" in _req_url:
                                import time as _t
                                video_segment_requests.append((_t.time(), _req_url))
                                if len(video_segment_requests) > 20:
                                    video_segment_requests.pop(0)
                                log.debug("Interceptor: video segment %s", _req_url[-80:])
                            if armed and mpd_info is None:
                                queue_candidate(
                                    _req_url,
                                    _request.get("headers", {}),
                                    "cdp_interceptor",
                                )
                        if _method != "Target.attachedToTarget":
                            continue
                        _params = _data.get("params", {})
                        _sid = _params.get("sessionId")
                        _tinfo = _params.get("targetInfo", {})
                        _ttype = _tinfo.get("type", "")
                        _turl = _tinfo.get("url", "")
                        _waiting = _params.get("waitingForDebugger", False)
                        if _ttype == "page":
                            log.info(
                                "CDP interceptor: new page target %s (waiting=%s)",
                                _turl[:80], _waiting,
                            )
                            # addScriptToEvaluateOnNewDocument runs in ALL frames
                            # (main + iframes) of this page target before their JS.
                            await _cdp_send(
                                "Page.addScriptToEvaluateOnNewDocument",
                                {"source": _MSE_PATCH_INLINE},
                                _sid,
                            )
                            # Also enable Network events so we capture video segment
                            # requests from ALL tabs, not just the first one.
                            await _cdp_send("Network.enable", {}, _sid)
                            log.info(
                                "CDP interceptor: MSE patch + Network enabled for page %s",
                                _turl[:80],
                            )
                        elif _ttype == "iframe":
                            log.info(
                                "CDP interceptor: iframe target %s (waiting=%s)",
                                _turl[:80], _waiting,
                            )
                            await _cdp_send(
                                "Runtime.evaluate",
                                {"expression": _MSE_PATCH_INLINE},
                                _sid,
                            )
                        # Always resume — targets paused by waitForDebuggerOnStart
                        # will hang forever if we don't call this.
                        if _waiting and _sid:
                            await _cdp_send("Runtime.runIfWaitingForDebugger", {}, _sid)
        except asyncio.CancelledError:
            pass
        except Exception as _exc:
            log.debug("CDP target interceptor error: %s", _exc)

    _cdp_interceptor_task = asyncio.create_task(_run_cdp_target_interceptor())
    # Give the interceptor a moment to connect and register setAutoAttach
    await asyncio.sleep(0.5)

    if first_run:
        print(f"\nOpening Amazon Prime Video in Chrome (first run — sign in to save session)...")
        print(f"  1. Dismiss any cookie banners")
        print(f"  2. Sign in to your Amazon account")
        print(f"  3. Click on a live channel and wait for playback to start")
        print(f"  4. Enable subtitles/closed captions if not already on")
    else:
        print(f"\nOpening Amazon Prime Video in Chrome (using saved session)...")
        print(f"  Click on a live channel and wait for the stream to start playing")
        print(f"  Enable subtitles/closed captions if not already on")
    print(f"  Waiting up to {timeout_s}s for MPD manifest traffic...\n")

    # Also register via Playwright's add_init_script as a belt-and-suspenders
    # fallback for any frames Playwright does track (first page's frames).
    await context.add_init_script(_MSE_PATCH_INLINE)
    for existing_page in context.pages:
        for frame in existing_page.frames:
            try:
                await frame.evaluate(_MSE_PATCH_INLINE)
                log.debug("MSE patch injected into existing frame: %s", frame.url[:80])
            except Exception as exc:
                log.debug("Frame patch error (%s): %s", frame.url[:60], exc)

    await page.goto(amazon_url, wait_until="domcontentloaded")

    # Optionally wait for the user to click into the target stream before
    # arming MPD capture (avoids grabbing the preview/trailer MPD).
    if wait_for_enter:
        print(
            "\n  >>> Click into the correct live stream and enable subtitles, "
            "then press Enter here to begin MPD capture..."
        )
        await asyncio.to_thread(input)
        # Clear video segment requests from preview content
        video_segment_requests.clear()
        print(f"  Capture armed. Waiting up to {timeout_s}s for MPD manifest...\n")
    armed = True

    try:
        await asyncio.wait_for(captured_event.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        validator_task.cancel()
        if _cdp_interceptor_task:
            _cdp_interceptor_task.cancel()
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()
        rejection_hint = (
            f" Last candidates: {' | '.join(candidate_rejections)}"
            if candidate_rejections else ""
        )
        raise TimeoutError(
            f"No MPD manifest captured within {timeout_s}s. "
            "Make sure the live stream is playing. "
            "If blocked, use --mpd-url instead."
            f"{rejection_hint}"
        )

    if mpd_info is None:
        validator_task.cancel()
        if _cdp_interceptor_task:
            _cdp_interceptor_task.cancel()
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()
        raise RuntimeError("mpd_info is None after capture")

    validator_task.cancel()

    result = CapturedAmazonSession(
        mpd_url=mpd_info["mpd_url"],
        headers=mpd_info["headers"],
    )
    log.info("Amazon session: mpd_url=%s", result.mpd_url[:120])

    recorder = None
    if record_audio:
        recorder = BrowserAudioRecorder(page, chrome_proc, browser, pw)
        # Share the video segment request log so the recorder can compute
        # stream latency from tfdt at recording start time.
        recorder._video_segment_requests = video_segment_requests
        recorder._amazon_mpd_url = mpd_info["mpd_url"]
        recorder._amazon_headers = mpd_info["headers"]
        recorder._debug_port = debug_port  # for per-tab WebSocket CDP reads
        # Keep interceptor alive while recording so future navigations
        # (e.g. channel switches) also get the MSE patch.
        recorder._cdp_interceptor_task = _cdp_interceptor_task
    else:
        if _cdp_interceptor_task:
            _cdp_interceptor_task.cancel()
        await browser.close()
        chrome_proc.terminate()
        await pw.stop()

    return result, recorder
