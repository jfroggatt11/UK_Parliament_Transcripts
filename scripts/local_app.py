#!/usr/bin/env python3
"""Local continuous BBC subtitle capture and dashboard. No core dependencies."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from build_dashboard import STOPWORDS, TOPICS

ROOT = Path(__file__).resolve().parents[1]
DAY_ZONE = ZoneInfo("Europe/London")
UTC = timezone.utc
BBC_BASE = "https://vs-cmaf-push-uk.live.fastly.md.bbci.co.uk"
SEGMENT_SECONDS = 3.84
TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
LOG = logging.getLogger("pulse")


def local_day(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, DAY_ZONE).date().isoformat()


def normalise(text: str) -> str:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def new_words(previous: str, current: str) -> list[str]:
    """Remove rolling subtitle overlap at word boundaries, preserving repetition."""
    old, new = previous.split(), current.split()
    old_keys = [w.casefold().strip(".,!?;:\"'’") for w in old]
    new_keys = [w.casefold().strip(".,!?;:\"'’") for w in new]
    for size in range(min(len(old), len(new)), 0, -1):
        if old_keys[-size:] == new_keys[:size]:
            return new[size:]
    return new


def parse_timestamp(value: str) -> float:
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_segment(data: bytes) -> list[tuple[float, float, str]]:
    """Read BBC's absolute-time TTML from m4s; preserve subtitle line breaks."""
    cues = []
    fragments = re.findall(rb"<(?:\w+:)?tt(?:\s|>).*?</(?:\w+:)?tt>", data, re.S)
    if not fragments:
        raise ValueError("Subtitle segment contained no TTML document")
    for fragment in fragments:
        root = ET.fromstring(fragment)
        for p in root.iter():
            if p.tag.rsplit("}", 1)[-1] != "p" or not p.get("begin"):
                continue
            for node in p.iter():
                if node.tag.rsplit("}", 1)[-1] == "br":
                    node.text = " "
            text = normalise("".join(p.itertext()))
            if text:
                begin = parse_timestamp(p.attrib["begin"])
                cues.append((begin, parse_timestamp(p.get("end", p.attrib["begin"])), text))
    return sorted(cues)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY, channel TEXT NOT NULL,
                    begin REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
                    UNIQUE(channel, begin, text)
                );
                CREATE INDEX IF NOT EXISTS cue_time ON cues(channel, begin);
                CREATE TABLE IF NOT EXISTS words (
                    id INTEGER PRIMARY KEY, cue_id INTEGER NOT NULL,
                    channel TEXT NOT NULL, stamp REAL NOT NULL,
                    day TEXT NOT NULL, hour INTEGER NOT NULL,
                    word TEXT NOT NULL, token TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS word_day ON words(channel, day);
                CREATE INDEX IF NOT EXISTS word_time ON words(channel, stamp);
                CREATE TABLE IF NOT EXISTS segments (
                    channel TEXT NOT NULL, segment TEXT NOT NULL,
                    PRIMARY KEY(channel, segment)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, channel: str, segment: str, cues: list[tuple[float, float, str]]) -> int:
        """Commit an entire segment atomically. Re-fetching it cannot double counts."""
        added = 0
        with self.connect() as db:
            if db.execute("SELECT 1 FROM segments WHERE channel=? AND segment=?", (channel, segment)).fetchone():
                return 0
            last = db.execute("SELECT begin, text FROM cues WHERE channel=? ORDER BY begin DESC, id DESC LIMIT 1", (channel,)).fetchone()
            previous, previous_time = (last["text"], last["begin"]) if last else ("", 0)
            for begin, end, raw in sorted(cues):
                text = normalise(raw)
                if not text:
                    continue
                cur = db.execute("INSERT OR IGNORE INTO cues(channel, begin, end, text) VALUES(?,?,?,?)", (channel, begin, end, text))
                if cur.rowcount != 1 or begin < previous_time:
                    continue
                # Don't remove words from a different programme after a long silence.
                delta = new_words(previous if begin - previous_time < 30 else "", text)
                dt = datetime.fromtimestamp(begin, DAY_ZONE)
                for word in delta:
                    for token in TOKEN_RE.findall(word):
                        db.execute("INSERT INTO words(cue_id,channel,stamp,day,hour,word,token) VALUES(?,?,?,?,?,?,?)", (cur.lastrowid, channel, begin, dt.date().isoformat(), dt.hour, token, token.casefold().replace("’", "'")))
                        added += 1
                previous, previous_time = text, begin
            db.execute("INSERT INTO segments VALUES(?,?)", (channel, segment))
        return added

    def live(self, channel: str, now: float) -> dict:
        with self.connect() as db:
            latest = db.execute("SELECT begin, end, text FROM cues WHERE channel=? ORDER BY begin DESC,id DESC LIMIT 1", (channel,)).fetchone()
            rows = db.execute("SELECT id,stamp,word FROM words WHERE channel=? AND stamp>=? ORDER BY id DESC LIMIT 100", (channel, now - 120)).fetchall()
        return {"cue": dict(latest) if latest and latest["begin"] >= now - 120 else None,
                "last_cue_at": latest["begin"] if latest else None,
                "words": [dict(row) for row in reversed(rows)]}

    def analytics(self, channel: str, days: int, now: float) -> dict:
        today = local_day(now)
        start = (datetime.fromtimestamp(now, DAY_ZONE).date() - timedelta(days=days - 1)).isoformat()
        with self.connect() as db:
            counts = Counter({r["token"]: r["n"] for r in db.execute("SELECT token,COUNT(*) n FROM words WHERE channel=? AND day=? GROUP BY token", (channel, today))})
            totals = {r["day"]: r["n"] for r in db.execute("SELECT day,COUNT(*) n FROM words WHERE channel=? AND day>=? AND day<=? GROUP BY day", (channel, start, today))}
            grouped = db.execute("SELECT day,hour,token,COUNT(*) n FROM words WHERE channel=? AND day>=? AND day<=? GROUP BY day,hour,token", (channel, start, today)).fetchall()
            # Only read today's ordered tokens for adjacent phrase counts.
            day_tokens = db.execute("SELECT token,stamp FROM words WHERE channel=? AND day=? ORDER BY id", (channel, today)).fetchall()
            coverage = db.execute("SELECT MIN(begin) first,MAX(begin) last,COUNT(*) cues FROM cues WHERE channel=?", (channel,)).fetchone()
        top = lambda c, limit: [{"word": w, "count": n} for w, n in c.most_common() if w not in STOPWORDS and len(w) > 2][:limit]
        timeline, history = [], Counter()
        bins = defaultdict(Counter)
        for r in grouped:
            bins[(r["day"], r["hour"])][r["token"]] = r["n"]
            if r["day"] < today:
                history[r["token"]] += r["n"]
        for (day, hour), frequencies in sorted(bins.items()):
            total = sum(frequencies.values())
            timeline.append({"day": day, "hour": hour, "words": total,
                             "topics": {name: round(sum(frequencies[t] for t in spec["terms"]) * 1000 / max(1, total), 1) for name, spec in TOPICS.items()}})
        baseline_total, today_total = sum(history.values()), sum(counts.values())
        rising = []
        if baseline_total:
            for word, n in counts.items():
                if word not in STOPWORDS and len(word) > 2 and n >= 5:
                    ratio = (n / max(1, today_total)) / ((history[word] + 1) / baseline_total)
                    if ratio >= 1.5:
                        rising.append({"word": word, "count": n, "ratio": round(ratio, 1)})
        phrases = Counter(f"{a['token']} {b['token']}" for a, b in zip(day_tokens, day_tokens[1:]) if a["token"] not in STOPWORDS and b["token"] not in STOPWORDS and len(a["token"]) > 2 and len(b["token"]) > 2 and b["stamp"] - a["stamp"] < 15)
        daily = []
        for offset in range(days):
            day = (datetime.fromisoformat(start).date() + timedelta(days=offset)).isoformat()
            daily.append({"day": day, "words": totals.get(day, 0)})
        return {"today": {"day": today, "words": today_total, "cloud": top(counts, 36), "phrases": top(phrases, 10)},
                "history": {"days": days, "daily": daily, "hourly": timeline, "rising": sorted(rising, key=lambda r: r["ratio"], reverse=True)[:12], "baseline_words": baseline_total,
                            "coverage": dict(coverage)},
                "topics": [{"name": name, "colour": spec["colour"]} for name, spec in TOPICS.items()]}


class Collector:
    def __init__(self, store: Store, args: argparse.Namespace):
        self.store, self.args = store, args
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = {"state": "starting" if not args.no_capture else "paused", "message": "Connecting to BBC Parliament" if not args.no_capture else "Capture is paused", "last_segment_at": None, "last_success_at": None, "failures": 0}
        self.base, self.x_param = args.bbc_base_url.rstrip("/"), args.x_param
        self.headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bbc.co.uk/iplayer/"}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def bootstrap(self):
        # Optional browser dependency; no private SubLQ dependency is imported.
        import sys
        sys.path.insert(0, str(ROOT / "vendor/Live_Subtitle_Scraper"))
        from src.browser import capture_session
        self.update(state="connecting", message="Sign in to BBC in the browser, start playback and enable subtitles")
        session, _ = asyncio.run(capture_session(self.args.channel, timeout_s=180, headless=self.args.headless, live_url="https://www.bbc.co.uk/iplayer/live/bbcparliament"))
        if session.channel != self.args.channel:
            raise ValueError(f"Browser returned {session.channel}, expected {self.args.channel}")
        self.base, self.x_param = session.base_url, session.x_param
        self.headers.update({k: v for k, v in session.headers.items() if k.lower() not in {"host", "accept-encoding", "range", "content-length"}})

    def run(self):
        failures, last_number, last_bootstrap = 0, None, 0.0
        if self.args.browser:
            try:
                self.bootstrap()
                last_bootstrap = time.monotonic()
            except Exception as exc:
                LOG.warning("Browser setup failed: %s", exc)
                self.update(state="error", message="Browser setup failed. Check terminal instructions; direct capture will be attempted.")
        while not self.stop.is_set():
            number = math.floor((time.time() - 25) / SEGMENT_SECONDS)
            if last_number is None:
                last_number = number - 2
            # Recover short interruptions from the CDN buffer; do not request an unbounded backlog.
            pending = range(max(last_number + 1, number - 8), number + 1)
            for segment in pending:
                if self.stop.is_set():
                    break
                url = f"{self.base}/x={self.x_param}/i=urn:bbc:pips:service:{self.args.channel}/t=3840/s=caption1/b=64000/{segment}.m4s"
                try:
                    with urlopen(Request(url, headers=self.headers), timeout=8) as response:
                        data = response.read(2_000_000)
                    cues = parse_segment(data)
                    if any(abs(begin - time.time()) > 300 for begin, _, _ in cues):
                        raise ValueError("Subtitle timestamps are not near the current broadcast")
                    self.store.ingest(self.args.channel, str(segment), cues)
                    failures = 0
                    stamp = time.time()
                    self.update(state="capturing", message="Receiving subtitle segments", failures=0, last_segment_at=stamp, last_success_at=stamp)
                    last_number = segment
                except HTTPError as exc:
                    failures += 1
                    if exc.code == 404 and failures < 4:
                        self.update(state="waiting", message="Waiting for the next subtitle segment", failures=failures)
                    else:
                        message = "BBC access was refused. Try browser setup or check access from this computer." if exc.code in (401, 403) else f"BBC returned HTTP {exc.code}; retrying"
                        self.update(state="error", message=message, failures=failures)
                    if exc.code in (401, 403) and self.args.browser and failures >= 3 and time.monotonic() - last_bootstrap > 300:
                        last_bootstrap = time.monotonic()
                        try:
                            self.bootstrap()
                        except Exception as refresh_exc:
                            LOG.warning("Browser refresh failed: %s", refresh_exc)
                    break
                except (URLError, TimeoutError, OSError, ValueError, ET.ParseError) as exc:
                    failures += 1
                    self.update(state="error", message="Subtitle connection interrupted; retrying", failures=failures)
                    LOG.warning("Capture failed: %s", exc)
                    break
                except Exception:
                    failures += 1
                    self.update(state="error", message="Capture failed; retrying. See terminal for details.", failures=failures)
                    LOG.exception("Unexpected collector failure")
                    break
            self.stop.wait(min(30, SEGMENT_SECONDS * max(1, min(failures, 8))))


def import_session(store: Store, session: Path, channel: str):
    metadata = session / "session.json"
    if metadata.exists():
        channel = json.loads(metadata.read_text()).get("channel", channel)
    files = sorted((session / "subs").glob("*.txt"))
    if not files:
        raise ValueError(f"No subtitle segment files found in {session / 'subs'}")
    segments = []
    for path in files:
        cues = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                begin = float(parts[0])
                cues.append((begin, float(parts[1]) if parts[1] else begin, parts[2]))
        if cues:
            segments.append((min(c[0] for c in cues), path.stem, cues))
    for _, name, cues in sorted(segments):
        store.ingest(channel, name, cues)
    LOG.info("Imported %d segments for %s", len(segments), channel)


def make_handler(store: Store, collector: Collector, channel: str):
    cache, lock = {}, threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "dist"), **kwargs)

        def log_message(self, format, *args):
            LOG.debug(format, *args)

        def do_GET(self):
            path = urlsplit(self.path)
            if not path.path.startswith("/api/"):
                return super().do_GET()
            now = time.time()
            try:
                if path.path == "/api/live":
                    data = {"now": now, "channel": channel, "collector": collector.snapshot(), **store.live(channel, now)}
                elif path.path == "/api/analytics":
                    days = int(parse_qs(path.query).get("days", ["7"])[0])
                    if days not in (7, 30, 90):
                        raise ValueError("days must be 7, 30 or 90")
                    with lock:
                        key = (days, local_day(now))
                        if key not in cache or now - cache[key][0] >= 15:
                            cache.clear()
                            cache[key] = (now, store.analytics(channel, days, now))
                        data = {"generated_at": cache[key][0], "timezone": "Europe/London", **cache[key][1]}
                elif path.path == "/api/health":
                    state = collector.snapshot()
                    data = {"server": "ok", "collector": state, "data_stale": not state["last_success_at"] or now - state["last_success_at"] > 60}
                else:
                    self.send_error(404)
                    return
                encoded = json.dumps(data, allow_nan=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except ValueError as exc:
                self.send_error(400, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                LOG.exception("API request failed")
                self.send_error(500)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "work/local")
    parser.add_argument("--channel", default="bbc_parliament", choices=["bbc_parliament"])
    parser.add_argument("--bbc-base-url", default=BBC_BASE)
    parser.add_argument("--x-param", type=int, default=4)
    parser.add_argument("--browser", action="store_true", help="Discover subtitle connection in a BBC browser session (requires Playwright)")
    parser.add_argument("--headless", action="store_true", help="Headless browser setup after initial sign-in")
    parser.add_argument("--no-capture", action="store_true", help="Serve existing local data without making BBC requests")
    parser.add_argument("--import-session", type=Path, help="Import an existing scraper session's subs/*.txt")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.data_dir / "pulse.sqlite3")
    if args.import_session:
        import_session(store, args.import_session, args.channel)
    collector = Collector(store, args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, collector, args.channel))
    server.daemon_threads = True
    worker = threading.Thread(target=collector.run, daemon=True, name="subtitle-collector")
    if not args.no_capture:
        worker.start()

    def stop(*_):
        collector.stop.set()
        # shutdown must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    LOG.info("Open http://127.0.0.1:%s — data: %s", args.port, store.path)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        collector.stop.set()
        server.server_close()
        if worker.is_alive():
            worker.join(timeout=10)


if __name__ == "__main__":
    main()
