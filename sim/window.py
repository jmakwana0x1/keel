"""Day-boundary blocks for the Phase 0 window: first block with timestamp >= each 00:00 UTC."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from rpc import block

END = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)
DAYS = 90
OUT = Path(__file__).resolve().parent.parent / "data" / "phase0_window.json"


def first_block_at(ts: int, lo: int, hi: int) -> int:
    while lo < hi:
        mid = (lo + hi) // 2
        if block(mid)["timestamp"] >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo


def main():
    start = END - dt.timedelta(days=DAYS)
    hi = 26_478_000
    bounds = []
    for d in range(DAYS + 1):
        day = start + dt.timedelta(days=d)
        ts = int(day.timestamp())
        # 12 s slots: start the search near the estimate
        est = hi - (int(END.timestamp()) - ts) // 12
        lo_b, hi_b = est - 20_000, min(est + 20_000, hi)
        b = first_block_at(ts, lo_b, hi_b)
        assert block(b)["timestamp"] >= ts > block(b - 1)["timestamp"], day
        bounds.append({"date": day.date().isoformat(), "block": b})
    OUT.write_text(json.dumps({"days": DAYS, "end_utc": END.isoformat(), "day_start_blocks": bounds}, indent=1) + "\n")
    print(bounds[0], bounds[-1])


if __name__ == "__main__":
    main()
