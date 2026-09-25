"""SubLQ metric computation — delegates to the sublq library.

All latency inputs are in seconds; the sublq library works in milliseconds,
so values are converted on the way in.
"""

from sublq import SubLQResult, compute_sublq_from_latencies

__all__ = ["SubLQResult", "compute_all"]


def compute_all(
    latencies_s: list[float],
    cps_values: list[float] | None = None,
    latency_per_cps_s: list[float] | None = None,
) -> SubLQResult:
    """Compute SubLQ from a list of latency values in seconds.

    Parameters
    ----------
    latencies_s:
        Per-event latency values in seconds.
    cps_values:
        Speech rate in characters per second for each measurement window.
        Must be paired with latency_per_cps_s.
    latency_per_cps_s:
        Mean latency in seconds at each speech rate in cps_values.
    """
    latencies_ms = [v * 1000.0 for v in latencies_s]
    latency_per_cps_ms = (
        [v * 1000.0 for v in latency_per_cps_s] if latency_per_cps_s is not None else None
    )
    try:
        return compute_sublq_from_latencies(
            latencies=latencies_ms,
            cps_values=cps_values,
            latency_per_cps=latency_per_cps_ms,
        )
    except OverflowError as exc:
        raise ValueError(
            f"SubLQ computation overflowed — latency values are likely unrealistically large "
            f"(n={len(latencies_ms)}, min={min(latencies_ms):.0f}ms, max={max(latencies_ms):.0f}ms). "
            f"Check anchor calibration. Original error: {exc}"
        ) from exc
