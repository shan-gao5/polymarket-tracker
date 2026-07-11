"""Deterministic time math for the 15-minute BTC Up/Down market series.

Every market in the series covers one wall-clock window of exactly 900
seconds, aligned to 15-minute boundaries in UTC. The market's Polymarket
slug encodes the window start as a unix timestamp: ``btc-updown-15m-{epoch}``.
Because the schedule is fully deterministic, the slug for any past, current,
or future window can be computed locally without calling any API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WINDOW_SECONDS = 15 * 60
SLUG_PREFIX = "btc-updown-15m"


def window_start_for(ts: float | datetime) -> int:
    """Align a timestamp down to the start of its 15-minute window.

    Accepts either a unix timestamp or an aware/naive ``datetime``
    (naive datetimes are interpreted as UTC). Returns the window start
    as a unix epoch in seconds.
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.timestamp()
    epoch = int(ts)
    return epoch - (epoch % WINDOW_SECONDS)


def slug_for(window_start_epoch: int) -> str:
    """Return the market slug for the window starting at ``window_start_epoch``."""
    if window_start_epoch % WINDOW_SECONDS != 0:
        raise ValueError(
            f"window start {window_start_epoch} is not aligned to "
            f"{WINDOW_SECONDS}-second boundaries"
        )
    return f"{SLUG_PREFIX}-{window_start_epoch}"


def epoch_for_slug(slug: str) -> int:
    """Extract the window-start epoch from a market slug."""
    prefix = f"{SLUG_PREFIX}-"
    if not slug.startswith(prefix):
        raise ValueError(f"slug {slug!r} does not belong to the {SLUG_PREFIX} series")
    try:
        epoch = int(slug[len(prefix):])
    except ValueError as exc:
        raise ValueError(f"slug {slug!r} does not end in a unix timestamp") from exc
    if epoch % WINDOW_SECONDS != 0:
        raise ValueError(f"slug {slug!r} is not aligned to {WINDOW_SECONDS}-second boundaries")
    return epoch


def window_bounds(window_start_epoch: int) -> tuple[datetime, datetime]:
    """Return the (start, end) UTC datetimes of a window."""
    start = datetime.fromtimestamp(window_start_epoch, tz=timezone.utc)
    return start, start + timedelta(seconds=WINDOW_SECONDS)
