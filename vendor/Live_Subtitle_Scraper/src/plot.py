"""Subtitle quality visualisations aligned to the Beyond Latency v2 framework.

Generates publication-quality plots from scraper and evaluation data:
  - Latency distribution (violin + box)
  - Latency time series with jitter
  - Speech rate vs delay (robustness analysis)
  - CDN latency comparison across broadcasters
  - Multi-session dashboard
  - Per-session word frequency

Usage:
    python -m src.plot session output/bbc/20260328_131517
    python -m src.plot compare output/bbc/20260328_131517 output/itv/20260327_193833
    python -m src.plot cdn output/bbc/20260328_131517 output/itv/20260327_193833
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "it", "to", "of", "in",
    "for", "on", "at", "by", "with", "that", "this", "was", "are", "be",
    "has", "have", "had", "not", "from", "as", "you", "he", "she", "they",
    "we", "his", "her", "its", "my", "your", "i", "me", "him", "us",
    "them", "do", "did", "so", "if", "up", "out", "no", "just", "will",
    "can", "all", "been", "there", "their", "what", "when", "who", "how",
    "very", "about", "into", "than", "then", "some", "would", "could",
    "going", "got", "get", "go", "now", "well", "here", "back", "over",
    "one", "two", "it's", "don't", "doesn't", "that's", "he's", "she's",
    "they're", "we're", "i'm", "you're", "there's", "were", "which",
}


# ── Data loading ─────────────────────────────────────────────────────

def _load_meta(session_dir: Path) -> dict:
    p = session_dir / "session.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _session_label(meta: dict, session_dir: Path) -> str:
    if meta:
        b = meta.get("broadcaster", "").upper()
        c = meta.get("channel", "")
        return f"{b} / {c}"
    return session_dir.name


def _load_latency_csv(session_dir: Path) -> list[dict]:
    p = session_dir / "latency.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def _load_alignment(session_dir: Path) -> list[dict]:
    p = session_dir / "alignment.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return list(csv.DictReader(f))


def _load_delay_points(session_dir: Path) -> list[tuple[float, float]]:
    """Load summary delay points, preferring collapsed subtitle events."""
    event_path = session_dir / "delay_events.csv"
    if event_path.exists():
        with open(event_path, newline="") as f:
            rows = list(csv.DictReader(f))
        points = [
            (float(r["asr_time"]), float(r["delay"]))
            for r in rows
            if r.get("asr_time") and r.get("delay")
        ]
        if points:
            return points

    rows = _load_alignment(session_dir)
    return [
        (float(r["asr_time"]), float(r["delay"]))
        for r in rows
        if r["delay"] and r["asr_time"]
    ]


def _rolling_avg(values: list[float], window: int) -> list[float]:
    return [
        sum(values[i - window:i]) / window
        for i in range(window, len(values) + 1)
    ]


# ── Figure 1: Latency distribution (violin + box) ───────────────────

def plot_latency_distribution(
    session_dirs: list[Path],
    show: bool = True,
    save_path: Path | None = None,
) -> None:
    """Violin + box plots of subtitle delay distributions across sessions.

    Mirrors Figure 1 from the paper: shows full density, median, IQR,
    and outliers for each platform/session.
    """
    import matplotlib.pyplot as plt

    all_delays = []
    labels = []
    for sd in session_dirs:
        delays = [d for _, d in _load_delay_points(sd)]
        if not delays:
            # Fall back to CDN latency from scraper data
            lat_rows = _load_latency_csv(sd)
            delays = [float(r["cdn_latency"]) for r in lat_rows if r["cdn_latency"]]
        if delays:
            all_delays.append(delays)
            meta = _load_meta(sd)
            labels.append(_session_label(meta, sd))

    if not all_delays:
        print("No delay data found.")
        return

    fig, ax = plt.subplots(figsize=(max(6, len(all_delays) * 2.5), 6))

    parts = ax.violinplot(all_delays, showmedians=False, showextrema=False)
    for pc in parts["bodies"]:
        pc.set_facecolor("steelblue")
        pc.set_alpha(0.4)

    bp = ax.boxplot(
        all_delays, widths=0.15, patch_artist=True,
        boxprops=dict(facecolor="white", edgecolor="black"),
        medianprops=dict(color="orangered", linewidth=2),
        whiskerprops=dict(color="black"),
        capprops=dict(color="black"),
        flierprops=dict(marker=".", markersize=2, alpha=0.3),
    )

    # Annotate medians
    for i, delays in enumerate(all_delays):
        med = statistics.median(delays)
        ax.annotate(
            f"{med:.1f}s", xy=(i + 1, med),
            xytext=(10, 5), textcoords="offset points",
            fontsize=9, color="orangered", fontweight="bold",
        )

    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("Subtitle Delay (seconds)")
    ax.set_title("Latency Distribution — Violin + Box Plot")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved: {save_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ── Figure 2: Latency time series with jitter ───────────────────────

def plot_latency_timeseries(
    session_dir: Path,
    rolling_window: int = 20,
    show: bool = True,
    save: bool = True,
) -> Path | None:
    """Time series of subtitle-event delay with rolling average.

    Mirrors Figure 2 from the paper: illustrates jitter patterns
    (periodic oscillation vs random walk) over a broadcast.
    Also shows jitter (|Δdelay|) as a shaded area.
    """
    import matplotlib.pyplot as plt

    points = _load_delay_points(session_dir)
    if not points:
        print("No alignment delay data.")
        return None

    meta = _load_meta(session_dir)
    label = _session_label(meta, session_dir)

    times = [t / 60 for t, _ in points]
    delays = [d for _, d in points]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1],
                                    sharex=True)

    # Top: delay time series
    ax1.scatter(times, delays, s=4, alpha=0.2, color="steelblue", rasterized=True)
    if rolling_window > 0 and len(delays) >= rolling_window:
        roll_d = _rolling_avg(delays, rolling_window)
        roll_t = times[rolling_window - 1:]
        ax1.plot(roll_t, roll_d, color="orangered", linewidth=2,
                 label=f"Rolling mean ({rolling_window}w)")
    ax1.axhline(statistics.mean(delays), color="gray", linestyle="--",
                linewidth=1, alpha=0.6, label=f"μL = {statistics.mean(delays):.2f}s")
    ax1.set_ylabel("Delay (seconds)")
    ax1.set_title(f"Latency Time Series — {label}")
    ax1.legend(loc="upper right", fontsize=9)
    ax1.grid(True, alpha=0.3)

    # Bottom: jitter (absolute consecutive difference)
    jitters = [abs(delays[i] - delays[i - 1]) for i in range(1, len(delays))]
    jitter_t = times[1:]
    if len(jitters) >= rolling_window:
        roll_j = _rolling_avg(jitters, rolling_window)
        roll_jt = jitter_t[rolling_window - 1:]
        ax2.fill_between(roll_jt, roll_j, alpha=0.3, color="darkorchid")
        ax2.plot(roll_jt, roll_j, color="darkorchid", linewidth=1.5,
                 label=f"Rolling jitter ({rolling_window}w)")
    ax2.axhline(statistics.mean(jitters), color="gray", linestyle="--",
                linewidth=1, alpha=0.6, label=f"JL = {statistics.mean(jitters):.3f}s")
    ax2.set_xlabel("Time (minutes)")
    ax2.set_ylabel("|Δdelay| (seconds)")
    ax2.set_title("Jitter (Consecutive Delay Change)")
    ax2.legend(loc="upper right", fontsize=9)
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    out_path = None
    if save:
        out_path = session_dir / "latency_timeseries.png"
        fig.savefig(out_path, dpi=150)
        print(f"Saved: {out_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)
    return out_path


# ── Figure 3: Speech rate vs delay (robustness) ─────────────────────

def plot_speech_rate_vs_delay(
    session_dir: Path,
    bin_duration_s: float = 30.0,
    show: bool = True,
    save: bool = True,
) -> Path | None:
    """Speech rate (WPM) vs mean delay per time bin.

    Mirrors Figure 3 from the paper: shows whether the respeaker's delay
    increases under high speech rates. Bins are coloured by speech rate
    category (Normal < 160, Elevated 160-200, Fast > 200 WPM).
    """
    import matplotlib.pyplot as plt
    import numpy as np

    rows = _load_alignment(session_dir)
    delay_points = _load_delay_points(session_dir)
    asr_times = [float(r["asr_time"]) for r in rows if r["asr_time"]]
    if not delay_points or not asr_times:
        print("No alignment data.")
        return None

    meta = _load_meta(session_dir)
    label = _session_label(meta, session_dir)
    max_t = max(asr_times)

    # Build time bins: speech rate + mean delay
    bins = []
    t = 0.0
    while t < max_t:
        t_end = t + bin_duration_s
        wpm = sum(1 for at in asr_times if t <= at < t_end) * (60 / bin_duration_s)
        ds = [d for at, d in delay_points if t <= at < t_end]
        mean_d = statistics.mean(ds) if ds else None
        if mean_d is not None:
            bins.append((t / 60, wpm, mean_d))
        t = t_end

    if not bins:
        print("No binned data.")
        return None

    wpms = [w for _, w, _ in bins]
    mean_ds = [d for _, _, d in bins]

    # Colour by speech rate category
    colours = []
    for w in wpms:
        if w < 160:
            colours.append("mediumseagreen")  # Normal
        elif w < 200:
            colours.append("orange")  # Elevated
        else:
            colours.append("crimson")  # Fast

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    # Left: scatter of WPM vs delay per bin
    ax1.scatter(wpms, mean_ds, c=colours, s=40, alpha=0.7, edgecolors="black", linewidth=0.3)
    ax1.set_xlabel("Speech Rate (WPM)")
    ax1.set_ylabel("Mean Delay (seconds)")
    ax1.set_title(f"Speech Rate vs Delay — {label}")
    ax1.grid(True, alpha=0.3)

    # Add legend
    from matplotlib.patches import Patch
    legend_items = [
        Patch(facecolor="mediumseagreen", label="Normal (<160 WPM)"),
        Patch(facecolor="orange", label="Elevated (160-200)"),
        Patch(facecolor="crimson", label="Fast (>200)"),
    ]
    ax1.legend(handles=legend_items, loc="upper left", fontsize=9)

    # Correlation line
    if len(wpms) >= 3:
        try:
            r = statistics.correlation(wpms, mean_ds)
            z = np.polyfit(wpms, mean_ds, 1)
            p = np.poly1d(z)
            x_line = np.linspace(min(wpms), max(wpms), 100)
            ax1.plot(x_line, p(x_line), "--", color="gray", alpha=0.6,
                     label=f"r = {r:.3f}")
            ax1.legend(handles=legend_items + [
                plt.Line2D([0], [0], linestyle="--", color="gray", label=f"r = {r:.3f}")
            ], loc="upper left", fontsize=9)
        except Exception:
            pass

    # Right: box plots by speech rate category
    normal = [d for w, d in zip(wpms, mean_ds) if w < 160]
    elevated = [d for w, d in zip(wpms, mean_ds) if 160 <= w < 200]
    fast = [d for w, d in zip(wpms, mean_ds) if w >= 200]

    box_data = []
    box_labels = []
    box_colours = []
    for data, lbl, col in [
        (normal, "Normal\n<160", "mediumseagreen"),
        (elevated, "Elevated\n160-200", "orange"),
        (fast, "Fast\n>200", "crimson"),
    ]:
        if data:
            box_data.append(data)
            box_labels.append(lbl)
            box_colours.append(col)

    if box_data:
        bp = ax2.boxplot(box_data, patch_artist=True, widths=0.5)
        for patch, col in zip(bp["boxes"], box_colours):
            patch.set_facecolor(col)
            patch.set_alpha(0.5)
        ax2.set_xticklabels(box_labels)
        ax2.set_ylabel("Mean Delay (seconds)")
        ax2.set_title("Delay by Speech Rate Category")
        ax2.grid(True, axis="y", alpha=0.3)

        # Annotate n
        for i, data in enumerate(box_data):
            ax2.annotate(f"n={len(data)}", xy=(i + 1, max(data)),
                         xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=9, color="gray")

    fig.tight_layout()
    out_path = None
    if save:
        out_path = session_dir / "speech_rate_vs_delay.png"
        fig.savefig(out_path, dpi=150)
        print(f"Saved: {out_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)
    return out_path


# ── CDN latency comparison across broadcasters ───────────────────────

def plot_cdn_latency(
    session_dirs: list[Path],
    show: bool = True,
    save_path: Path | None = None,
) -> None:
    """Violin + box of CDN latency (transport-level) across sessions."""
    import matplotlib.pyplot as plt

    all_cdn = []
    labels = []
    for sd in session_dirs:
        rows = _load_latency_csv(sd)
        cdn = [float(r["cdn_latency"]) for r in rows if r["cdn_latency"]]
        if cdn:
            all_cdn.append(cdn)
            meta = _load_meta(sd)
            labels.append(_session_label(meta, sd))

    if not all_cdn:
        print("No CDN latency data.")
        return

    fig, ax = plt.subplots(figsize=(max(6, len(all_cdn) * 2.5), 6))
    parts = ax.violinplot(all_cdn, showmedians=False, showextrema=False)
    for pc in parts["bodies"]:
        pc.set_facecolor("teal")
        pc.set_alpha(0.4)

    bp = ax.boxplot(
        all_cdn, widths=0.15, patch_artist=True,
        boxprops=dict(facecolor="white", edgecolor="black"),
        medianprops=dict(color="orangered", linewidth=2),
        flierprops=dict(marker=".", markersize=2, alpha=0.3),
    )
    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("CDN Latency (seconds)")
    ax.set_title("CDN Transport Latency — Broadcaster Comparison")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved: {save_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ── Per-session dashboard (delay + speech rate + coverage + words) ───

def plot_session_dashboard(
    session_dir: Path,
    rolling_window: int = 20,
    top_n_words: int = 25,
    rate_bin_s: float = 30.0,
    show: bool = True,
    save: bool = True,
) -> Path | None:
    """4-panel dashboard for a single session.

    Panels: delay timeseries, speech rate vs delay, coverage, word frequency.
    """
    import matplotlib.pyplot as plt

    rows = _load_alignment(session_dir)
    if not rows:
        print("No alignment.csv found. Run src.evaluate first.")
        return None

    meta = _load_meta(session_dir)
    title = _session_label(meta, session_dir)
    sid = meta.get("session_id", session_dir.name)

    # Extract data
    delay_pts = _load_delay_points(session_dir)
    asr_times_all = []
    asr_words = []
    sub_words = []
    match_flags = []

    for r in rows:
        if r["asr_time"]:
            t = float(r["asr_time"])
            asr_times_all.append(t)
            if r["asr_word"]:
                asr_words.append(r["asr_word"])
            match_flags.append((t, r["kind"] == "match"))
        if r["sub_word"]:
            sub_words.append(r["sub_word"])
    if not delay_pts:
        print("No delay data.")
        return None

    d_t = [t / 60 for t, _ in delay_pts]
    d_v = [d for _, d in delay_pts]

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(f"{title} — {sid}", fontsize=14, fontweight="bold")

    # ── Panel 1: Delay time series ──
    ax = axes[0, 0]
    ax.scatter(d_t, d_v, s=4, alpha=0.2, color="steelblue", rasterized=True)
    if len(d_v) >= rolling_window:
        roll_d = _rolling_avg(d_v, rolling_window)
        ax.plot(d_t[rolling_window - 1:], roll_d, color="orangered", linewidth=2,
                label=f"Rolling mean ({rolling_window}w)")
    mu = statistics.mean(d_v)
    ax.axhline(mu, color="gray", linestyle="--", linewidth=1, alpha=0.6,
               label=f"μL = {mu:.2f}s")
    ax.set_ylabel("Delay (s)")
    ax.set_title("Subtitle Delay Over Time")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── Panel 2: Speech rate vs delay ──
    ax = axes[0, 1]
    max_t = max(asr_times_all) if asr_times_all else 1
    bins_t, bins_wpm, bins_delay = [], [], []
    t = 0.0
    while t < max_t:
        t_end = t + rate_bin_s
        wpm = sum(1 for at in asr_times_all if t <= at < t_end) * (60 / rate_bin_s)
        ds = [d for at, d in delay_pts if t <= at < t_end]
        if ds:
            bins_t.append(t / 60)
            bins_wpm.append(wpm)
            bins_delay.append(statistics.mean(ds))
        t = t_end

    colours = ["mediumseagreen" if w < 160 else "orange" if w < 200 else "crimson"
               for w in bins_wpm]
    ax.scatter(bins_wpm, bins_delay, c=colours, s=30, alpha=0.7,
               edgecolors="black", linewidth=0.3)
    ax.set_xlabel("Speech Rate (WPM)")
    ax.set_ylabel("Mean Delay (s)")
    ax.set_title("Speech Rate vs Delay")
    ax.grid(True, alpha=0.3)
    if len(bins_wpm) >= 3:
        try:
            r = statistics.correlation(bins_wpm, bins_delay)
            ax.annotate(f"r = {r:.3f}", xy=(0.05, 0.95), xycoords="axes fraction",
                        fontsize=10, color="gray", va="top")
        except Exception:
            pass

    # ── Panel 3: Coverage over time ──
    ax = axes[1, 0]
    if match_flags and len(match_flags) >= rolling_window:
        cov_t = [t / 60 for t, _ in match_flags]
        cov_v = [1.0 if m else 0.0 for _, m in match_flags]
        roll_cov = _rolling_avg(cov_v, rolling_window)
        roll_cov_t = cov_t[rolling_window - 1:]
        ax.plot(roll_cov_t, [c * 100 for c in roll_cov],
                color="darkorchid", linewidth=1.5)
        ax.fill_between(roll_cov_t, [c * 100 for c in roll_cov],
                        alpha=0.15, color="darkorchid")
        ax.set_ylim(0, 105)
    ax.set_xlabel("Time (minutes)")
    ax.set_ylabel("Coverage (%)")
    ax.set_title(f"Subtitle Coverage (rolling {rolling_window}w)")
    ax.grid(True, alpha=0.3)

    # ── Panel 4: Word frequency ──
    ax = axes[1, 1]
    asr_clean = [_PUNCT_RE.sub("", w.lower()).strip() for w in asr_words]
    sub_clean = [_PUNCT_RE.sub("", w.lower()).strip() for w in sub_words]
    asr_freq = Counter(w for w in asr_clean if w and w not in _STOPWORDS)
    sub_freq = Counter(w for w in sub_clean if w and w not in _STOPWORDS)

    combined = Counter()
    for w, c in asr_freq.items():
        combined[w] += c
    for w, c in sub_freq.items():
        combined[w] += c
    top = [w for w, _ in combined.most_common(top_n_words)]
    top.reverse()

    y_pos = range(len(top))
    ax.barh(y_pos, [asr_freq.get(w, 0) for w in top], height=0.4,
            align="edge", color="steelblue", alpha=0.7, label="ASR")
    ax.barh([y - 0.4 for y in y_pos], [sub_freq.get(w, 0) for w in top],
            height=0.4, align="edge", color="coral", alpha=0.7, label="Subtitles")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(top, fontsize=8)
    ax.set_xlabel("Count")
    ax.set_title(f"Top {top_n_words} Words")
    ax.legend(fontsize=8)

    fig.tight_layout()
    out_path = None
    if save:
        out_path = session_dir / "dashboard.png"
        fig.savefig(out_path, dpi=150)
        print(f"Saved: {out_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)
    return out_path


# ── SubLQ metrics comparison table ───────────────────────────────────

def plot_metrics_table(
    session_dirs: list[Path],
    show: bool = True,
    save_path: Path | None = None,
) -> None:
    """Bar chart comparing SubLQ dimensions across sessions.

    Mirrors Table 3 from the paper: μL, σL, JL, CV, L95 side by side.
    """
    import matplotlib.pyplot as plt
    import numpy as np

    entries = []
    for sd in session_dirs:
        ep = sd / "evaluation.json"
        if not ep.exists():
            continue
        ev = json.loads(ep.read_text())
        meta = _load_meta(sd)
        label = _session_label(meta, sd)
        sublq = ev.get("sublq", {})
        entries.append({
            "label": label,
            "μL": ev.get("mean_delay_s", 0),
            "σL": ev.get("delay_std_s", 0),
            "JL": sublq.get("j_l", 0),
            "CV": sublq.get("cv", 0),
            "L95": ev.get("p95_delay_s", 0),
        })

    if not entries:
        print("No evaluation.json files found.")
        return

    dims = ["μL", "σL", "JL", "CV", "L95"]
    labels = [e["label"] for e in entries]
    n_sessions = len(entries)
    n_dims = len(dims)

    fig, axes = plt.subplots(1, n_dims, figsize=(3 * n_dims, 5), sharey=True)
    if n_dims == 1:
        axes = [axes]

    colours = plt.cm.Set2(np.linspace(0, 1, n_sessions))

    for ax, dim in zip(axes, dims):
        vals = [e[dim] for e in entries]
        bars = ax.barh(range(n_sessions), vals, color=colours, edgecolor="black",
                       linewidth=0.5)
        ax.set_title(dim, fontsize=12, fontweight="bold")
        ax.set_yticks(range(n_sessions))
        ax.set_yticklabels(labels if ax == axes[0] else [])
        ax.grid(True, axis="x", alpha=0.3)

        # Annotate values
        for bar, v in zip(bars, vals):
            fmt = f"{v:.3f}" if dim == "CV" else f"{v:.2f}"
            ax.text(bar.get_width() + 0.05, bar.get_y() + bar.get_height() / 2,
                    fmt, va="center", fontsize=9)

    fig.suptitle("SubLQ Dimensions — Broadcaster Comparison", fontsize=14, fontweight="bold")
    fig.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150)
        print(f"Saved: {save_path}")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Subtitle quality plots (Beyond Latency v2 framework)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Session dashboard
    p_sess = sub.add_parser("session", help="Per-session 4-panel dashboard")
    p_sess.add_argument("session_dir", type=Path)
    p_sess.add_argument("--rolling", type=int, default=20)
    p_sess.add_argument("--top-words", type=int, default=25)
    p_sess.add_argument("--no-show", action="store_true")

    # Timeseries (delay + jitter)
    p_ts = sub.add_parser("timeseries", help="Delay + jitter time series")
    p_ts.add_argument("session_dir", type=Path)
    p_ts.add_argument("--rolling", type=int, default=20)
    p_ts.add_argument("--no-show", action="store_true")

    # Speech rate analysis
    p_sr = sub.add_parser("speech-rate", help="Speech rate vs delay")
    p_sr.add_argument("session_dir", type=Path)
    p_sr.add_argument("--bin", type=float, default=30.0, help="Bin duration in seconds")
    p_sr.add_argument("--no-show", action="store_true")

    # Compare distributions
    p_cmp = sub.add_parser("compare", help="Compare delay distributions across sessions")
    p_cmp.add_argument("session_dirs", type=Path, nargs="+")
    p_cmp.add_argument("--no-show", action="store_true")
    p_cmp.add_argument("-o", "--output", type=Path, default=None)

    # CDN latency
    p_cdn = sub.add_parser("cdn", help="Compare CDN latency across sessions")
    p_cdn.add_argument("session_dirs", type=Path, nargs="+")
    p_cdn.add_argument("--no-show", action="store_true")
    p_cdn.add_argument("-o", "--output", type=Path, default=None)

    # Metrics table
    p_met = sub.add_parser("metrics", help="SubLQ dimension comparison")
    p_met.add_argument("session_dirs", type=Path, nargs="+")
    p_met.add_argument("--no-show", action="store_true")
    p_met.add_argument("-o", "--output", type=Path, default=None)

    args = parser.parse_args()

    if args.command == "session":
        plot_session_dashboard(
            args.session_dir, rolling_window=args.rolling,
            top_n_words=args.top_words, show=not args.no_show,
        )
    elif args.command == "timeseries":
        plot_latency_timeseries(
            args.session_dir, rolling_window=args.rolling,
            show=not args.no_show,
        )
    elif args.command == "speech-rate":
        plot_speech_rate_vs_delay(
            args.session_dir, bin_duration_s=args.bin,
            show=not args.no_show,
        )
    elif args.command == "compare":
        plot_latency_distribution(
            args.session_dirs, show=not args.no_show,
            save_path=args.output or Path("compare_distributions.png"),
        )
    elif args.command == "cdn":
        plot_cdn_latency(
            args.session_dirs, show=not args.no_show,
            save_path=args.output or Path("cdn_latency.png"),
        )
    elif args.command == "metrics":
        plot_metrics_table(
            args.session_dirs, show=not args.no_show,
            save_path=args.output or Path("metrics_comparison.png"),
        )


if __name__ == "__main__":
    main()
