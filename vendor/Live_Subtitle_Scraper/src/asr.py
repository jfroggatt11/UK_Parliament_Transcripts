"""ASR transcription using faster-whisper or whisperx.

Transcribes a WAV file and returns word-level timestamps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AsrWord:
    """A single word from ASR with timing relative to audio start."""
    start: float   # seconds from audio start
    end: float
    word: str


def transcribe(
    wav_path: Path,
    model_size: str = "base",
    engine: str = "whisperx",
) -> list[AsrWord]:
    """Transcribe a WAV file and return word-level timestamps.

    Args:
        wav_path: Path to 16kHz mono WAV file.
        model_size: Whisper model size (tiny, base, small, medium, large-v3).
        engine: 'whisperx' (default, faster with forced alignment) or
                'faster-whisper' (original backend).

    Returns:
        List of AsrWord sorted by start time.
    """
    if engine == "whisperx":
        return _transcribe_whisperx(wav_path, model_size)
    elif engine == "faster-whisper":
        return _transcribe_faster_whisper(wav_path, model_size)
    else:
        raise ValueError(f"Unknown ASR engine: {engine!r}. Use 'whisperx' or 'faster-whisper'.")


def _transcribe_whisperx(wav_path: Path, model_size: str) -> list[AsrWord]:
    """Transcribe using WhisperX (batched inference + forced alignment)."""
    import whisperx

    device = "cpu"
    compute_type = "int8"

    log.info("Loading WhisperX model '%s' (device=%s)...", model_size, device)
    model = whisperx.load_model(model_size, device, compute_type=compute_type)

    log.info("Transcribing %s...", wav_path.name)
    audio = whisperx.load_audio(str(wav_path))
    result = model.transcribe(audio, batch_size=16, language="en")

    # Forced alignment for word-level timestamps
    log.info("Aligning words...")
    model_a, metadata = whisperx.load_align_model(language_code="en", device=device)
    result = whisperx.align(
        result["segments"], model_a, metadata, audio, device,
    )

    words: list[AsrWord] = []
    for segment in result["segments"]:
        for w in segment.get("words", []):
            # whisperx may omit start/end for some words
            if "start" not in w or "end" not in w:
                continue
            words.append(AsrWord(
                start=w["start"],
                end=w["end"],
                word=w["word"].strip(),
            ))

    log.info("ASR complete: %d words", len(words))
    return words


def _transcribe_faster_whisper(wav_path: Path, model_size: str) -> list[AsrWord]:
    """Transcribe using faster-whisper (original backend)."""
    from faster_whisper import WhisperModel

    log.info("Loading Whisper model '%s'...", model_size)
    model = WhisperModel(model_size, compute_type="int8")

    log.info("Transcribing %s...", wav_path.name)
    segments, info = model.transcribe(
        str(wav_path),
        language="en",
        word_timestamps=True,
        vad_filter=True,
    )

    words: list[AsrWord] = []
    for segment in segments:
        if segment.words is None:
            continue
        for w in segment.words:
            words.append(AsrWord(start=w.start, end=w.end, word=w.word.strip()))

    log.info("ASR complete: %d words, %.1fs audio", len(words), info.duration)
    return words
