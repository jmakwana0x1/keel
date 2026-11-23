# Derivations

Units are stated per quantity. "raw" means the token's smallest unit (wei for WETH, 1e-6 for USDC).
`√p` is the pool's `sqrtPriceX96 / 2^96`, so `p` is token1-raw per token0-raw.

## Depth walk (`depth_reference.walk`)

Given state `(√p, tick, L, book)` and target `√p_t`, repeat:

1. `next` = next initialized tick in the swap direction **within one 256-bit bitmap word**, else the word boundary.
2. Step target `√p_s = min(√p(next), √p_t)` going up, `max(…)` going down.
3. Step input (pre-fee, rounded up):
   - up, token1 in: `Δy = ⌈L·(√p_s − √p)·2^96 / 2^96⌉` in raw token1
   - down, token0 in: `Δx = ⌈⌈L·2^96·(√p − √p_s) / √p⌉ / √p_s⌉` in raw token0
4. Step fee: `⌈Δ · f / (10^6 − f)⌉`, with `f` in pips.
5. If the step reached an initialized tick, cross it: `L ← L ± liquidityNet`.

The walk is an exact-output swap with an unbounded amount, so every step reaches its target. In the step where an exact-input swap reaches the same price limit, the input and fee are identical. Rounding is per step, so the word-boundary splits in (1) change the total, and the reference reproduces them.

A step where `L = 0` costs zero in both input and fee. That is how a pool crosses a void, and it is why true depth saturates.

## Naive depth

`N_up = L·(√p_t − √p)` in raw token1, and `N_down = L·(1/√p_t − 1/√p)` in raw token0, with `L` held at its spot value. For small `x`, `√(1+x) − 1 ≈ x/2`, so `N` is linear in `x` to first order.

## Saturation ratio

`N(x) / D(x)`, both pre-fee and in the same raw token. Above 1, the naive figure overstates the input actually required.

## ε

`log V_it = ε · log D_it + β_σ · log σ_it + β_p · log P_it + α_i + γ_t + u_it`

- `V`: daily volume in USD, quote-token leg.
- `D`: mean over hourly samples of `(D_up + D_down)/2` in USD.
- `σ`: daily realised volatility, `√Σ r_h²` over hourly log-returns of `p`. Dimensionless, per day.
- `P`: base-token USD price.
- `α_i`, `γ_t`: pool and day fixed effects.

## `k_arb`

LVR rate for an in-range position of liquidity `L`: `ℓ = σ²·p²/2·|x'(p)|`, with `x(p) = L/√p + c`, which gives `ℓ = σ²·L·√p/4`. Here `σ²` is the per-block variance of `log p` and the result is in raw token1 per block.

- Per hourly sample interval of 300 blocks: `LVR = σ²_blk · L · √p / 4 · 300`.
- `σ²_blk` is the day's realised variance divided by the day's block count.

Realised markout per swap, from the pool's side, excluding fees:

`gain = (a0 − f0)·p(t+h) + (a1 − f1)` in raw token1, where `a` is the signed amount the pool receives and `f = a·fee` on the input leg only.

`k_arb(h) = Σ(−gain) / Σ LVR` over the window, both in USD at the hour's price.

The fee per swap is approximated as `a_in · fee`. The exact fee is rounded up per step, which differs by at most 1 raw unit per step, negligible at the totals reported.
