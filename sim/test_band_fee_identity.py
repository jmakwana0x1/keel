"""The band-fee identity used by band_economics.py, f/(1-f)·L·Δ√p (up) and f/(1-f)·L·Δ(1/√p) (down), checked
against the exact integer reference walk on a book that holds only the band. Agreement is to rounding:
the walk rounds every step's input and fee up, so the walk may exceed the identity by a few raw units."""
import random
from depth_reference import Book, Q96, sqrt_at_tick, walk

rng = random.Random(3)
worst = 0
for trial in range(400):
    ts = rng.choice([10, 60, 200])
    fee = {10: 500, 60: 3000, 200: 10000}[ts]
    lo_t = rng.randrange(-20000, 20000) // ts * ts
    hi_t = lo_t + ts * rng.randrange(1, 40)
    L = rng.randrange(10**12, 10**22)
    b = Book(sqrt_price=0, tick=0, liquidity=L, tick_spacing=ts, fee=fee, ticks={lo_t: [L, L], hi_t: [L, -L]})
    s_lo, s_hi = sqrt_at_tick(lo_t), sqrt_at_tick(hi_t)
    a = rng.randrange(s_lo + 1, s_hi - 1)
    b.sqrt_price, b.tick = a, lo_t  # tick only needs to be inside [lo, hi) for the walk's bitmap stepping
    while sqrt_at_tick(b.tick + 1) <= a:
        b.tick += 1
    c = rng.randrange(s_lo + 1, s_hi - 1)
    if c == a:
        continue
    w, _ = walk(b, c)
    fr = fee / (1e6 - fee)
    ident = fr * L * ((c - a) / Q96 if c > a else (Q96 / c - Q96 / a))
    rel = abs(w.fee - ident) / max(ident, 1)
    worst = max(worst, rel if ident > 1e6 else 0)
    assert ident < 1e6 or rel < 1e-9, (trial, w.fee, ident)
print(f"400 random bands: identity matches exact walk fee; worst relative gap {worst:.2e} (rounding only)")
