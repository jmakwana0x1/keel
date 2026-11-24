# Open questions

Questions that would need **new measurement**. Standing rule (2026-11-24): append here and keep building. A question that blocks a gate is raised for a decision instead of being worked.

| # | Question | Why it matters | Blocks a gate? |
|---|---|---|---|
| 1 | How many mid-cap pools exist on Base, Arbitrum v4 and Unichain? | DefiLlama reports no volume there. 31 non-blue-chip pools with TVL ≥ $1M are unclassified, so the 26-pool market figure is known only from Ethereum and Arbitrum v3. **Do not count** (owner decision). | No |
| 2 | Why does ENA classify mid-cap on the 90-day median but long-tail on the latest 7 days? | It is the single proxy miss (14 of 15). If it is a trend rather than noise, the 7-day proxy undercounts declining pools. | No |
| 3 | `k_arb` against a CEX reference rather than the pool's own mid | D0.6(4): pool-mid `k_arb` understates external LVR on stale books, which is the direction that could flip marginal pools. | No |
| 4 | Market-making deal pricing from a real source | The $120k–$600k anchor was retracted as unmeasured (D0.15 §4). The Gate 0 back-solve sub-item remains open. | No |
| 5 | Intra-hour withdraw-and-return (JIT) | D0.11's hourly sampling nets it out, so "LPs do not flee" holds only at hourly resolution. | No |
| 6 | Robustness of the re-centring gap across windows | Phase 0.5 is one 90-day window with one major ETH drawdown. | No |
| 7 | Re-centring cost through the cheapest venue rather than the pool itself | The venue-free bound (−3.22%) caps the effect. The realistic figure lies between the two. | No |
