"""Build transcripts from per-segment subtitle text files.

Produces two transcript formats:
  - Word-level: one word per line with timestamp
  - Chunk-level: one phrase/sentence per line with timestamp range

Usage:
    python -m src.transcript output/bbc/20260324_212814
"""

import re
import sys

from pathlib import Path


def load_cues(subs_dir: Path) -> list[tuple[float, float, str]]:
    """Load all cues from segment files in order. Returns (begin, end, text).

    Applies spacing fixes on load so all downstream processing sees
    consistent word boundaries.
    """
    cues = []
    for f in sorted(subs_dir.iterdir()):
        if not f.suffix == ".txt":
            continue
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            begin = float(parts[0])
            end = float(parts[1]) if parts[1] else begin
            text = _fix_spacing(parts[2])
            cues.append((begin, end, text))
    # Sort by begin time
    cues.sort(key=lambda c: c[0])
    return cues


def _fix_spacing(text: str) -> str:
    """Fix missing spaces where BBC concatenates two display lines.

    BBC's rolling cues join two display rows without a separator,
    producing "backtrying" instead of "back trying".  We insert a
    space before any uppercase letter that follows a lowercase letter
    or punctuation, which covers the vast majority of cases.

    The lowercase→lowercase join ("backtrying") is harder to fix
    automatically without a dictionary, so we leave it for now —
    it only affects the first word at each line break.
    """
    # "fairnessNusken" → "fairness Nusken"  (lowercase→uppercase)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # "it.It" → "it. It"  (punctuation→letter)
    text = re.sub(r"([.!?,;:])([A-Za-z])", r"\1 \2", text)
    return text


def _find_new_text(prev: str, cur: str) -> str:
    """Find text in cur that is genuinely new compared to prev.

    Uses character-level suffix/prefix matching to handle BBC's
    joined words (e.g. "backtrying" in prev, "trying" in cur).
    """
    if not prev:
        return cur
    if cur == prev:
        return ""

    # Simple append: cur starts with prev
    if cur.startswith(prev):
        return cur[len(prev):].lstrip()

    # Find longest suffix of prev that matches a prefix of cur
    # Start from the longest possible and work down
    max_len = min(len(prev), len(cur))
    for k in range(max_len, 0, -1):
        if prev.endswith(cur[:k]):
            new = cur[k:].lstrip()
            return new

    # No overlap — entirely new text
    return cur


def build_word_transcript(cues: list[tuple[float, float, str]]) -> list[tuple[float, str]]:
    """Extract individual words by diffing consecutive cues.

    Each BBC cue adds one word to a rolling display. By comparing
    consecutive cues, we extract the newly added word and its timestamp.
    """
    words: list[tuple[float, str]] = []
    prev_text = ""

    for begin, end, text in cues:
        new_text = _find_new_text(prev_text, text)
        for w in new_text.split():
            words.append((begin, w))
        prev_text = text

    return words


def build_chunk_transcript(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Build phrase-level transcript from word-level cues.

    Uses the word transcript to reconstruct clean, non-overlapping chunks
    split at natural sentence boundaries (., !, ?).
    """
    words = build_word_transcript(cues)
    if not words:
        return []

    chunks: list[tuple[float, float, str]] = []
    chunk_start = words[0][0]
    chunk_words: list[str] = []

    for ts, word in words:
        chunk_words.append(word)
        # Split at sentence-ending punctuation
        if word.endswith((".","!","?")) and len(chunk_words) >= 3:
            text = " ".join(chunk_words)
            chunks.append((chunk_start, ts, text))
            chunk_words = []
            chunk_start = ts  # next chunk starts after this word

    # Emit remaining words
    if chunk_words:
        text = " ".join(chunk_words)
        chunks.append((chunk_start, words[-1][0], text))

    return chunks


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.transcript <session_dir>")
        sys.exit(1)

    session_dir = Path(sys.argv[1])
    subs_dir = session_dir / "subs"
    if not subs_dir.exists():
        print(f"No subs/ directory in {session_dir}")
        sys.exit(1)

    cues = load_cues(subs_dir)
    print(f"Loaded {len(cues)} cues from {len(list(subs_dir.glob('*.txt')))} segments\n")

    if not cues:
        print("No subtitle cues found. Check that --record-av was used and subtitles were enabled.")
        sys.exit(1)

    # Reference time for relative timestamps
    t0 = cues[0][0]

    def _rel(ts: float) -> str:
        """Format as HH:MM:SS.mmm relative to session start."""
        s = ts - t0
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:06.3f}"

    # Word-level transcript
    words = build_word_transcript(cues)
    word_path = session_dir / "transcript_words.txt"
    with open(word_path, "w", encoding="utf-8") as f:
        for ts, word in words:
            f.write(f"{_rel(ts)}\t{word}\n")
    print(f"Word-level transcript: {word_path} ({len(words)} words)")

    # Chunk-level transcript
    chunks = build_chunk_transcript(cues)
    chunk_path = session_dir / "transcript_chunks.txt"
    with open(chunk_path, "w", encoding="utf-8") as f:
        for begin, end, text in chunks:
            f.write(f"{_rel(begin)}\t{_rel(end)}\t{text}\n")
    print(f"Chunk-level transcript: {chunk_path} ({len(chunks)} chunks)")

    # Print preview
    print(f"\n--- Word-level (first 30 words) ---")
    for ts, word in words[:30]:
        print(f"  {_rel(ts)}  {word}")

    print(f"\n--- Chunk-level (first 10 chunks) ---")
    for begin, end, text in chunks[:10]:
        print(f"  {_rel(begin)} – {_rel(end)}  {text}")


if __name__ == "__main__":
    main()
