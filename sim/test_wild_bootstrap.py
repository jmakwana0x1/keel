"""Synthetic check of model._wcr_ci: 23 clusters with strong within-cluster error correlation and a
known slope. Reports coverage of the CRV1-t CI vs the wild-cluster CI over repeated panels."""
import time
import numpy as np
import pandas as pd
import model

model.BOOT_B = 1999
rng = np.random.default_rng(1)
TRUE = 0.4
hits_crv = hits_wild = 0
R = 200
t = time.time()
for rep in range(R):
    G, T = 23, 90
    pool = np.repeat(np.arange(G), T); day = np.tile(np.arange(T), G)
    # heavy-tailed cluster sizes in x-variance: a few clusters dominate, the case CRV1 under-covers
    scale = rng.lognormal(0, 0.8, G)[pool]
    x = rng.normal(size=G)[pool] + scale * (rng.normal(size=G * T) + rng.normal(size=G)[pool] * 1.5)
    u = scale * (rng.normal(size=G)[pool] * 1.0 + rng.normal(size=G * T) * 0.5) * (1 + 0.3 * np.tanh(x))
    y = TRUE * x + rng.normal(size=G)[pool] + u
    d = pd.DataFrame({"pool": [f"p{p:02d}" for p in pool], "date": day, "volume_usd": np.exp(y),
                      "depth1_usd": np.exp(x), "sigma_day": np.exp(rng.normal(size=G * T)),
                      "base_usd": np.exp(rng.normal(size=G * T))})
    e = model.elasticity(d, "depth1_usd")
    hits_crv += e["ci95_crv1_t"][0] <= TRUE <= e["ci95_crv1_t"][1]
    hits_wild += e["ci95_wild_cluster"][0] <= TRUE <= e["ci95_wild_cluster"][1]
print(f"coverage over {R} panels: CRV1-t {hits_crv/R:.2f}, wild cluster {hits_wild/R:.2f}  ({time.time()-t:.0f}s)")
