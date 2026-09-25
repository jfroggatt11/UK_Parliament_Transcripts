#!/usr/bin/env python3
"""Turn a Live Subtitle Scraper session into dashboard JSON.

The topic layer is deliberately local and deterministic so hourly refreshes do
not need a paid model API.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

STOPWORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "are", "as", "at", "be",
    "been", "before", "being", "but", "by", "can", "could", "did", "do", "does", "for",
    "from", "get", "got", "had", "has", "have", "he", "her", "here", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "just", "more", "most", "my", "of", "on",
    "one", "or", "our", "out", "said", "say", "she", "so", "some", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "to", "too", "up", "us", "was",
    "we", "were", "what", "when", "where", "which", "who", "will", "with", "would", "you",
    "your", "it's", "that's", "we're", "they're", "i'm", "you'll",
}

TOPICS = {
    "Economy & cost of living": {"colour": "#e9b949", "terms": {"economy", "economic", "money", "cost", "costs", "tax", "taxes", "budget", "growth", "jobs", "business", "trade", "price", "prices", "wages", "billion", "million", "spending", "debt", "income"}},
    "Health & public services": {"colour": "#57c7b6", "terms": {"health", "nhs", "hospital", "hospitals", "doctor", "doctors", "care", "patients", "school", "schools", "education", "children", "housing", "homes", "services", "social", "funding", "staff", "waiting"}},
    "Security & foreign affairs": {"colour": "#ec7b60", "terms": {"war", "defence", "defense", "security", "military", "army", "ukraine", "russia", "israel", "gaza", "iran", "border", "terror", "police", "crime", "international", "foreign", "sanctions", "nato", "peace"}},
    "Parliament & government": {"colour": "#8ca8ff", "terms": {"parliament", "parliamentary", "government", "minister", "ministers", "ministerial", "commons", "lords", "mp", "mps", "speaker", "bill", "bills", "amendment", "amendments", "committee", "committees", "question", "questions", "vote", "votes", "election", "policy"}},
}

PARLIAMENTS = {
    "UK Parliament": {"terms": {"westminster", "house of commons", "house of lords", "commons", "lords", "mp", "mps", "speaker", "whitehall"}},
    "Scottish Parliament": {"terms": {"holyrood", "scottish parliament", "scotland", "msps", "edinburgh", "first minister"}},
    "Senedd Cymru": {"terms": {"senedd", "welsh parliament", "wales", "ms", "cardiff"}},
    "Northern Ireland Assembly": {"terms": {"stormont", "northern ireland assembly", "northern ireland", "mlas", "belfast"}},
    "European Parliament": {"terms": {"european parliament", "meps", "brussels", "strasbourg"}},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--session", type=Path, help="Session directory containing transcript_chunks.txt and session.json")
    p.add_argument("--output", type=Path, default=Path("dist/data/dashboard.json"))
    return p.parse_args()


def latest_session() -> Path:
    roots = [
        Path("/Users/jonahfroggatt/Documents/Tortoise/subtitles/Live_Subtitle_Scraper/output/bbc"),
        Path("/Users/jonahfroggatt/Documents/Tortoise/subtitles/Live_Subtitle_Scraper/analysis"),
        Path("output/bbc"), Path("analysis"),
    ]
    candidates = []
    for root in roots:
        if root.exists():
            candidates.extend(p.parent for p in root.rglob("transcript_chunks.txt"))
    if not candidates:
        raise SystemExit("No session found. Pass --session PATH.")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_session(session: Path) -> dict:
    path = session / "session.json"
    return json.loads(path.read_text()) if path.exists() else {}


def parse_clock(value: str) -> float:
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("\\n", " ")).strip()
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def load_chunks(session: Path) -> list[dict]:
    path = session / "transcript_chunks.txt"
    chunks = []
    if not path.exists():
        return chunks
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        try:
            start, end = parse_clock(parts[0]), parse_clock(parts[1])
        except ValueError:
            continue
        text = clean_text(parts[2])
        if text:
            chunks.append({"start": start, "end": end, "text": text})
    return chunks


def tokens(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in re.findall(r"[A-Za-z][A-Za-z’'-]{2,}", text)]


def infer_parliament(channel: str, text: str) -> tuple[str, float]:
    haystack = f"{channel.replace('_', ' ')} {text}".lower()
    scores = {name: sum(haystack.count(term) for term in spec["terms"]) for name, spec in PARLIAMENTS.items()}
    name, score = max(scores.items(), key=lambda item: item[1])
    if score == 0:
        return ("No chamber detected", 0.42 if "parliament" in channel else 0.18)
    return name, min(0.98, 0.52 + score * 0.08)


def format_clock(seconds: float) -> str:
    return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


def build_data(session: Path) -> dict:
    meta = load_session(session)
    chunks = load_chunks(session)
    if not chunks:
        raise SystemExit(f"No transcript_chunks.txt content in {session}")
    channel = meta.get("channel", "bbc_parliament")
    channel_label = channel.replace("_", " ").title()
    full_text = " ".join(c["text"] for c in chunks)
    parliament, confidence = infer_parliament(channel, full_text)
    counts = Counter(t for t in tokens(full_text) if t not in STOPWORDS and not t.isnumeric())
    top_words = [{"word": word, "count": count} for word, count in counts.most_common(36)]
    total_seconds = max(c["end"] for c in chunks) - min(c["start"] for c in chunks)
    bin_seconds = 900
    first = min(c["start"] for c in chunks)
    bins: dict[int, list[dict]] = defaultdict(list)
    for chunk in chunks:
        bins[int((chunk["start"] - first) // bin_seconds)].append(chunk)
    topic_series, word_series = [], []
    tracked_words = [w["word"] for w in top_words[:8]]
    for index in range(max(bins) + 1):
        bin_text = " ".join(c["text"] for c in bins.get(index, []))
        bin_counts = Counter(tokens(bin_text))
        label_seconds = index * bin_seconds
        label = f"{int(label_seconds // 3600):02d}:{int(label_seconds % 3600 // 60):02d}"
        topic_series.append({"label": label, "values": {name: sum(bin_counts[t] for t in spec["terms"]) for name, spec in TOPICS.items()}})
        word_series.append({"label": label, "values": {word: bin_counts[word] for word in tracked_words}})
    transcript = [{"time": format_clock(c["start"]), "end": format_clock(c["end"]), "text": c["text"]} for c in chunks]
    return {
        "meta": {"session_id": meta.get("session_id", session.name), "channel": channel, "channel_label": channel_label, "parliament": parliament, "parliament_confidence": confidence, "start_time": meta.get("start_time"), "end_time": meta.get("end_time"), "generated_at": datetime.now(timezone.utc).isoformat(), "source_session": str(session), "method": "Local lexical topic signals over 15-minute bins"},
        "summary": {"words": sum(counts.values()), "chunks": len(chunks), "duration_minutes": round(total_seconds / 60), "top_topic": max(TOPICS, key=lambda name: sum(v["values"][name] for v in topic_series)) if topic_series else "—"},
        "word_cloud": top_words,
        "topic_series": topic_series,
        "word_series": word_series,
        "topics": [{"name": name, "colour": spec["colour"]} for name, spec in TOPICS.items()],
        "transcript": transcript,
    }


def main() -> None:
    args = parse_args()
    session = args.session or latest_session()
    data = build_data(session)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {args.output} from {session}")


if __name__ == "__main__":
    main()
