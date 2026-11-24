"""Regenerate every Phase 0 result from the committed measured inputs in data/. No network.

Inputs  : data/phase0_universe.json, data/phase0_pool_days.csv, data/phase0_saturation_books.csv,
          data/phase0_quoter_diff.json
Outputs : data/phase0_results.json, data/phase0_results.md

CI runs this and fails on `git diff data/`. The artifact asserts its own preconditions: it refuses to
emit if the quoter differential has any mismatch or the saturation sample is under 100 pool-blocks.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MID_THRESHOLD_USD = 500_000  # docs/ARCHITECTURE.md D0.3, fixed before data
HORIZONS = [1, 25, 300]
XLAB = ["0.5", "1", "2", "5", "10"]


def r(v, n=4):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), n)


def classify(pools: list[dict], days: pd.DataFrame) -> dict[str, str]:
    med = days.groupby("pool")["volume_usd"].median()
    out = {}
    for p in pools:
        if p["group"] == "blue":
            out[p["pool"]] = "blue-chip"
        else:
            out[p["pool"]] = "mid-cap" if med.get(p["pool"], 0) >= MID_THRESHOLD_USD else "long-tail"
    return out


MIN_CLUSTERS = 10  # below this no cluster-robust CI is published (D0.7 addendum)
WEBB = np.array([-np.sqrt(1.5), -1.0, -np.sqrt(0.5), np.sqrt(0.5), 1.0, np.sqrt(1.5)])
BOOT_B, BOOT_SEED = 9999, 20261123


def _wcr_ci(x, y, Z, groups, alpha=0.05):
    """Wild cluster restricted (WCR) bootstrap CI for the coefficient on x, by test inversion, Webb weights.

    FWL: x and y are partialled on Z (fixed effects + controls) once. Under H0: beta = b0 the bootstrap
    outcome is  y* = b0·x~ + M[(y~ − b0·x~) ⊙ v],  v = cluster weights expanded to rows, which is linear in
    b0. So every per-draw quantity (beta*, the cluster scores) is an affine function of b0 whose two
    coefficient blocks are computed once; each p-value evaluation is then O(G·B)."""
    Q, _ = np.linalg.qr(Z)
    M = lambda v: v - Q @ (Q.T @ v)
    xt, yt = M(x), M(y)
    sxx = xt @ xt
    G = int(groups.max()) + 1
    N, K = len(y), np.linalg.matrix_rank(Z) + 1
    c = G / (G - 1) * (N - 1) / (N - K)
    H = np.zeros((N, G)); H[np.arange(N), groups] = 1.0
    rng = np.random.default_rng(BOOT_SEED)
    W = WEBB[rng.integers(0, 6, size=(G, BOOT_B))]  # common random numbers across b0
    # A = M(y~ ⊙ HW), B = M(x~ ⊙ HW); keep only x~ᵀA, x~ᵀB and the cluster sums Hᵀ(x~ ⊙ A), Hᵀ(x~ ⊙ B)
    xa, xb = np.empty(BOOT_B), np.empty(BOOT_B)
    HxA, HxB = np.empty((G, BOOT_B)), np.empty((G, BOOT_B))
    for k in range(0, BOOT_B, 500):
        V = H @ W[:, k:k + 500]
        A, B = M(yt[:, None] * V), M(xt[:, None] * V)
        xa[k:k + 500], xb[k:k + 500] = xt @ A, xt @ B
        HxA[:, k:k + 500], HxB[:, k:k + 500] = H.T @ (xt[:, None] * A), H.T @ (xt[:, None] * B)
    Hxx = H.T @ (xt * xt)

    bhat = (xt @ yt) / sxx
    sg_hat = H.T @ (xt * (yt - bhat * xt))
    se_hat = np.sqrt(c * (sg_hat ** 2).sum() / sxx ** 2)

    def pval(b0):
        t_obs = (bhat - b0) / se_hat
        bstar = b0 + (xa - b0 * xb) / sxx
        sg = b0 * Hxx[:, None] + HxA - b0 * HxB - bstar[None, :] * Hxx[:, None]
        tstar = (bstar - b0) / np.sqrt(c * (sg ** 2).sum(axis=0) / sxx ** 2)
        # relative tie tolerance: with few clusters draws tie with t_obs exactly, and float noise must not decide them
        return ((np.abs(tstar) >= abs(t_obs) * (1 - 1e-9)).sum() + 1) / (BOOT_B + 1)

    def bound(direction):
        inside, outside = bhat, bhat + direction * 4 * se_hat
        while pval(outside) >= alpha:
            outside += direction * 4 * se_hat
        for _ in range(40):
            mid = (inside + outside) / 2
            inside, outside = (mid, outside) if pval(mid) >= alpha else (inside, mid)
        return inside

    return float(bhat), float(se_hat), (bound(-1), bound(+1))


def elasticity(df: pd.DataFrame, depth_col: str, lag: bool = False) -> dict:
    d = df.copy().sort_values(["pool", "date"])
    if lag:
        d[depth_col] = d.groupby("pool")[depth_col].shift(1)
    d = d[(d.volume_usd > 0) & (d[depth_col] > 0) & (d.sigma_day > 0) & (d.base_usd > 0)].dropna(subset=[depth_col])
    if d.pool.nunique() < 3:
        return {"n_obs": len(d), "n_pools": int(d.pool.nunique()), "eps": None}
    d = d.assign(lv=np.log(d.volume_usd), ld=np.log(d[depth_col]), ls=np.log(d.sigma_day), lp=np.log(d.base_usd))
    m = smf.ols("lv ~ ld + ls + lp + C(pool) + C(date)", data=d).fit(
        cov_type="cluster", cov_kwds={"groups": pd.factorize(d.pool)[0]}, use_t=True)
    lo, hi = m.conf_int().loc["ld"]
    G = int(d.pool.nunique())
    if G < MIN_CLUSTERS:
        return {"eps": r(m.params["ld"]), "ci95": None, "ci95_crv1_t": None, "ci95_wild_cluster": None,
                "ci_declined": f"{G} clusters < {MIN_CLUSTERS}: no cluster-robust CI is reliable",
                "n_obs": int(len(d)), "n_pools": G}
    Z = np.column_stack([d.ls, d.lp, pd.get_dummies(d.pool).to_numpy(float),
                         pd.get_dummies(d.date, drop_first=True).to_numpy(float)])
    b, _, (wlo, whi) = _wcr_ci(d.ld.to_numpy(), d.lv.to_numpy(), Z, pd.factorize(d.pool)[0])
    assert abs(b - m.params["ld"]) <= 1e-6 * max(1.0, abs(b)), f"FWL {b} disagrees with statsmodels {m.params['ld']}"
    return {"eps": r(m.params["ld"]), "se_crv1": r(m.bse["ld"]), "ci95_crv1_t": [r(lo), r(hi)],
            "ci95_wild_cluster": [r(wlo), r(whi)],
            "ci95": [r(min(lo, wlo)), r(max(hi, whi))],  # the wider of the two: what the branch rule reads
            "n_obs": int(len(d)), "n_pools": int(d.pool.nunique())}


def vol_depth(cls: dict) -> dict:
    """Phase 0.4 (D0.11): effective liquidity in top-decile volatility hours vs the rest, decomposed into
    active (mint/burn) and passive (price drift through a fixed book), plus a declustered event study
    against a calm-hour placebo."""
    v = pd.read_csv(DATA / "phase0_vol_depth.csv")
    out = {"by_class": {}, "events": {}, "pools_top_decile_lower": {}, "thresholds_note":
           "Decile cut, 24 h baseline, 95% recovery level, 24 h trough window, 72 h horizon and the calm-hour "
           "placebo (|r| <= pool median) were chosen for this analysis, not pre-registered."}
    frames = []
    for pool, g in v.groupby("pool"):
        g = g.sort_values("i").reset_index(drop=True)
        g["absr"] = (2 * np.log(g.sqrt.astype(float)).diff()).abs()
        for x in ("1", "5"):
            base = g[f"leff{x}"].shift(1).rolling(24, min_periods=24).median()
            g[f"base{x}"] = base
            g[f"rel{x}"] = g[f"leff{x}"] / base - 1
            g[f"act{x}"] = g[f"leff{x}"] - g[f"frozen{x}"]            # absolute, in liquidity units
            g[f"pas{x}"] = g[f"frozen{x}"] - g[f"leff{x}"].shift(1)
        g["top"] = g.absr >= g.absr.quantile(0.9)
        g["cls"] = cls[pool]
        g["pool"] = pool
        frames.append(g)
    a = pd.concat(frames)
    a = a[np.isfinite(a.rel1) & np.isfinite(a.rel5)].dropna(subset=["absr", "act1", "act5", "pas1", "pas5"])
    for c, g in a.groupby("cls"):
        out["by_class"][c] = {}
        for x in ("1", "5"):
            t, o = g[g.top], g[~g.top]
            # active / passive as ratio of sums (Σ change / Σ baseline): robust to near-zero baselines on thin books
            rs = lambda d, col: r(d[col].sum() / d[f"base{x}"].sum())
            out["by_class"][c][x] = {
                "hours_top": int(len(t)), "hours_rest": int(len(o)),
                "median_rel_top": r(t[f"rel{x}"].median()), "median_rel_rest": r(o[f"rel{x}"].median()),
                "p10_rel_top": r(t[f"rel{x}"].quantile(.1)), "p10_rel_rest": r(o[f"rel{x}"].quantile(.1)),
                "active_top": rs(t, f"act{x}"), "active_rest": rs(o, f"act{x}"),
                "passive_top": rs(t, f"pas{x}"), "passive_rest": rs(o, f"pas{x}")}
    # tail hours: depth > 20% below baseline at ±1%. How much of that hour's own change was mint/burn?
    out["tail_hours"] = {}
    big = a[a.rel1 < -0.20]
    for (c, t), g in big.groupby(["cls", "top"]):
        ch = g.act1 + g.pas1
        out["tail_hours"].setdefault(c, {})["top_decile" if t else "rest"] = {
            "hours": int(len(g)), "median_hour_change": r((ch / g.base1).median()),
            "median_active_share_of_hour_change": r((g.act1 / ch).where(ch < 0).median()),
            "share_hours_active_exceeds_passive_loss": r((g.act1 < g.pas1).mean())}
    # stress context: worst peak-to-trough move of each pool's price over the window (either direction of quote)
    out["stress"] = {}
    for g in frames:
        lp = 2 * np.log(g.sqrt.astype(float))
        out["stress"][g.pool.iloc[0]] = {"max_abs_hourly_return": r(lp.diff().abs().max()),
                                         "worst_drawdown": r(np.expm1((lp - lp.cummax()).min())),
                                         "worst_runup": r(np.expm1((lp - lp.cummin()).max()))}
    for x in ("1", "5"):
        lower = [g[g.top][f"rel{x}"].median() < g[~g.top][f"rel{x}"].median() for _, g in a.groupby("pool")]
        out["pools_top_decile_lower"][x] = {"pools": int(sum(lower)), "of": len(lower)}
    rng = np.random.default_rng(20261123)
    ev = []
    for g in frames:
        pool = g.pool.iloc[0]
        calm = g.absr <= g.absr.median()
        orders = {"vol": list(g[g.top].sort_values("absr", ascending=False).index),
                  "calm": list(rng.permutation(g[calm].index.to_numpy()))}
        for kind, order in orders.items():
            taken = []
            for i in order:
                if i + 72 < len(g) and np.isfinite(g.loc[i, "base1"]) and np.isfinite(g.loc[i, "base5"]) \
                        and all(abs(i - j) >= 24 for j in taken):
                    taken.append(i)
            for i in taken:
                row = {"cls": cls[pool], "kind": kind}
                for x in ("1", "5"):
                    path = g.loc[i:i + 72, f"leff{x}"].to_numpy() / g.loc[i, f"base{x}"]
                    k0 = int(np.argmin(path[:25]))
                    trough = path[k0]
                    rec = next((k for k in range(k0, 73) if path[k] >= 0.95), None)
                    row[f"trough{x}"] = trough - 1
                    row[f"recovery{x}"] = (rec if trough < 0.95 else 0)
                    row[f"unrecovered{x}"] = bool(trough < 0.95 and rec is None)
                ev.append(row)
    e = pd.DataFrame(ev)
    for (c, kind), g in e.groupby(["cls", "kind"]):
        out["events"].setdefault(c, {})[kind] = {}
        for x in ("1", "5"):
            dd = g[g[f"trough{x}"] < -0.05]
            out["events"][c][kind][x] = {
                "events": int(len(g)), "median_trough": r(g[f"trough{x}"].median()),
                "p10_trough": r(g[f"trough{x}"].quantile(.1)),
                "share_trough_below_minus_5pct": r((g[f"trough{x}"] < -0.05).mean()),
                "share_trough_below_minus_20pct": r((g[f"trough{x}"] < -0.20).mean()),
                "median_recovery_hours_given_drawdown": r(dd[f"recovery{x}"].dropna().median()) if len(dd) else None,
                "share_unrecovered_72h_given_drawdown": r(dd[f"unrecovered{x}"].mean()) if len(dd) else None}
    return out


def recentre_gap(cls: dict, name: dict) -> dict:
    """Phase 0.5 (D0.12 as amended by D0.13): R_N − P per pool, decomposed, as % of capital; classification on the
    15 target pools at the headline configuration (x = 5%, N = 300) using D0.12's registered thresholds."""
    d = pd.read_csv(DATA / "phase0_recentre.csv", dtype={"x_pct": str})
    declined = sorted({name[p] for p in d[d.curator == "declined"].pool})
    d = d[d.curator != "declined"]
    rows = []
    for (pool, sizing, x), g in d.groupby(["pool", "sizing", "x_pct"]):
        P = g[g.curator == "P"].iloc[0]
        C = float(P.capital_usd)
        for _, R in g[g.curator != "P"].iterrows():
            benefit = (R.fees_usd - P.fees_usd) / C
            rebal, gas = R.rebalance_cost_usd / C, R.gas_usd / C
            gap = (R.total_usd - P.total_usd) / C          # gas included
            market = gap - benefit + gas + rebal
            rows.append({"pool": pool, "name": name[pool], "cls": cls[pool], "sizing": sizing, "x": x,
                         "N": int(R.curator.split("_")[1]), "recentres": int(R.recentres), "skipped": int(R.skipped),
                         "size_ratio": float(P.size_ratio_at_deploy), "capital_usd": C,
                         "benefit": benefit, "rebalance": rebal, "gas": gas, "market": market,
                         "cost_incl_gas": gas + rebal - market, "cost_excl_gas": rebal - market,
                         "gap_incl_gas": gap, "gap_excl_gas": gap + gas, "gas_usd": float(R.gas_usd)})
    t = pd.DataFrame(rows)
    out = {"declined_pools": declined, "headline": {}, "grid": {}, "per_pool_headline": []}
    for sizing, gk, ck in (("registered", "gap_incl_gas", "cost_incl_gas"), ("small", "gap_excl_gas", "cost_excl_gas")):
        h = t[(t.sizing == sizing) & (t.x == "5") & (t.N == 300) & (t.cls != "blue-chip")]
        med_gap, med_b, med_c = h[gk].median(), h.benefit.abs().median(), h[ck].abs().median()
        pos, neg, n = int((h[gk] > 0).sum()), int((h[gk] < 0).sum()), len(h)
        if med_b < 0.01 and med_c < 0.01:
            verdict = "KILL: no economic content (median |benefit| and |cost| both < 1% of C)"
        elif med_gap > 0.005 and pos >= 2 * n / 3:
            verdict = "coordination failure: benefit > cost"
        elif med_gap < -0.005 and neg >= 2 * n / 3:
            verdict = "priced: benefit < cost"
        else:
            verdict = "thin"
        out["headline"][sizing] = {
            "gap_basis": gk, "target_pools": n, "median_gap_pct": r(100 * med_gap), "pools_gap_positive": pos,
            "pools_gap_negative": neg, "median_abs_benefit_pct": r(100 * med_b), "median_abs_cost_pct": r(100 * med_c),
            "median_benefit_pct": r(100 * h.benefit.median()), "median_rebalance_pct": r(100 * h.rebalance.median()),
            "median_market_pct": r(100 * h.market.median()), "verdict": verdict,
            # venue-independent bound: the same gap if rebalancing cost nothing at all
            "median_gap_if_rebalance_free_pct": r(100 * (h[gk] + h.rebalance).median()),
            "pools_positive_if_rebalance_free": int(((h[gk] + h.rebalance) > 0).sum()),
            "pools_band_larger_than_pool": int((h.size_ratio >= 1).sum())}
        for (c, x, N), g in t[t.sizing == sizing].groupby(["cls", "x", "N"]):
            out["grid"].setdefault(sizing, {}).setdefault(c, {}).setdefault(x, {})[str(N)] = {
                "median_gap_pct": r(100 * g[gk].median()), "pools_positive": int((g[gk] > 0).sum()), "pools": int(len(g)),
                "median_benefit_pct": r(100 * g.benefit.median()), "median_cost_pct": r(100 * g[ck].median())}
    hs = t[(t.sizing == "small") & (t.x == "5") & (t.N == 300)].sort_values(["cls", "name"])
    for _, q in hs.iterrows():
        reg = t[(t.sizing == "registered") & (t.pool == q.pool) & (t.x == "5") & (t.N == 300)].iloc[0]
        out["per_pool_headline"].append({
            "pool": q["name"], "class": q.cls, "recentres": q.recentres, "skipped": q.skipped,
            "benefit_pct": r(100 * q.benefit), "rebalance_pct": r(100 * q.rebalance), "market_pct": r(100 * q.market),
            "gap_excl_gas_pct": r(100 * q.gap_excl_gas), "gas_usd_total": r(q.gas_usd),
            "capital_where_gas_is_half_pct_usd": r(q.gas_usd / 0.005, 0),
            "registered_gap_pct": r(100 * reg.gap_incl_gas), "registered_size_ratio": r(reg.size_ratio),
            # a ±5% band holding the pool's whole median active liquidity, in USD at deployment (small band ÷ 1%)
            "pool_scale_usd": r(q.capital_usd / 0.01, 0)})
    # control group: managed owners (D0.12)
    m = pd.read_csv(DATA / "phase0_managed.csv")
    has = m.dropna(subset=["owner"])
    sim = t[(t.sizing == "small") & (t.x == "5") & (t.N == 300)].set_index("pool")
    p_y = d[(d.sizing == "small") & (d.x_pct == "5")].assign(y=lambda q: q.fees_usd / (q.capital_usd * 90))
    out["managed"] = {}
    for c in ("blue-chip", "mid-cap", "long-tail"):
        pools_c = [p for p in cls if cls[p] == c]
        mc = has[has.pool.isin(pools_c)]
        yP = p_y[(p_y.pool.isin(mc.pool)) & (p_y.curator == "P")].y
        yR = p_y[(p_y.pool.isin(mc.pool)) & (p_y.curator == "R_300")].y
        out["managed"][c] = {
            "pools": len(pools_c), "pools_with_managed_owners": int(mc.pool.nunique()), "managed_owners": int(len(mc)),
            "recentre_events": int(mc.recentres.sum()),
            "median_owner_latency_blocks": r(mc.median_latency_blocks.median(), 0) if len(mc) else None,
            "median_owner_active_share": r(mc.median_active_share.median()) if len(mc) else None,
            "median_owner_fee_yield_per_day": r(mc.fee_yield_per_day.median(), 6) if len(mc) else None,
            "sim_P_fee_yield_per_day": r(yP.median(), 6) if len(yP) else None,
            "sim_R300_fee_yield_per_day": r(yR.median(), 6) if len(yR) else None,
            "median_sim_gap_pct_where_managed": r(100 * sim.loc[sorted(set(mc.pool))].gap_excl_gas.median()) if len(mc) else None,
            "median_sim_gap_pct_where_not_managed": r(100 * sim.loc[sorted(set(pools_c) - set(mc.pool))].gap_excl_gas.median())
            if set(pools_c) - set(mc.pool) else None}
    return out


def branch(e: dict) -> dict:
    """docs/ARCHITECTURE.md D0.5: reads the lower bound of the wider of the CRV1-t and wild-cluster CIs;
    a CI straddling a threshold takes the more conservative branch."""
    if e.get("eps") is None or e.get("ci95") is None:
        return {"branch": "undetermined", "reason": "insufficient pools"}
    lo = e["ci95"][0]
    if lo > 0.5:
        return {"branch": "3a-skim", "region": "eps > 0.5", "reason": "CI lower bound above 0.5"}
    if lo >= 0.3:
        return {"branch": "3a-skim-low-share", "region": "0.3 <= eps <= 0.5", "reason": "CI lower bound in [0.3, 0.5]"}
    return {"branch": "3b-mev-funded", "region": "eps < 0.3 not excluded", "reason": f"CI lower bound {lo} below 0.3"}


def main():
    pools = json.loads((DATA / "phase0_universe.json").read_text())["pools"]
    qd = json.loads((DATA / "phase0_quoter_diff.json").read_text())
    assert qd["mismatches"] == 0, "replayed walk disagrees with QuoterV2; refusing to emit results"
    assert qd["walks_with_word_boundary_steps"] > 0, "differential never exercised a word-boundary step"
    for mname, mres in qd["mutations_on_same_sample"].items():
        assert mres["failing"] > 0, f"differential cannot see planted mutation '{mname}'; refusing to emit results"
    days = pd.read_csv(DATA / "phase0_pool_days.csv")
    sat = pd.read_csv(DATA / "phase0_saturation_books.csv", dtype={"x_pct": str})
    cls = classify(pools, days)
    name = {p["pool"]: f'{p["base"]}/{p["quote"]} {p["fee"] / 1e4:g}%' for p in pools}
    days["cls"] = days.pool.map(cls)
    sat["cls"] = sat.pool.map(cls)

    res = {"window": json.loads((DATA / "phase0_window.json").read_text())["end_utc"],
           "classes": {name[k]: v for k, v in sorted(cls.items(), key=lambda kv: (kv[1], name[kv[0]]))},
           "quoter_differential": {k: qd[k] for k in ("walks", "mismatches", "quoter_declined_zero_depth",
                                                      "walks_with_word_boundary_steps", "mutations_on_same_sample")}}

    # ---- 0.1 epsilon
    eps = {}
    subsets = {"non-blue-chip": days[days.cls != "blue-chip"], "blue-chip": days[days.cls == "blue-chip"],
               "mid-cap": days[days.cls == "mid-cap"], "long-tail": days[days.cls == "long-tail"], "all": days}
    for sname, sub in subsets.items():
        eps[sname] = {"depth_1pct": elasticity(sub, "depth1_usd"), "depth_5pct": elasticity(sub, "depth5_usd"),
                      "depth_1pct_lagged": elasticity(sub, "depth1_usd", lag=True)}
    decision = branch(eps["non-blue-chip"]["depth_1pct"])
    decision_5 = branch(eps["non-blue-chip"]["depth_5pct"])
    res["epsilon"] = eps
    res["epsilon_branch"] = {"on_depth_1pct": decision, "on_depth_5pct": decision_5,
                             "caveat": "association, not causal: depth and volume are jointly determined"}

    # ---- epsilon sensitivity to a causal discount (D0.10). eps_breakeven = (s + phi) / (1 - phi), as specified
    e1 = eps["non-blue-chip"]["depth_1pct"]
    sens = []
    for disc in EPS_DISCOUNTS:
        de = {"eps": r(e1["eps"] * disc), "ci95": [r(e1["ci95"][0] * disc), r(e1["ci95"][1] * disc)]}
        cells = {}
        for sk in SKIM_GRID:
            for ph in SHARE_GRID:
                be = (sk + ph) / (1 - ph)
                cells[f"s={sk},phi={ph}"] = {"breakeven": r(be), "lower_bound_clears": bool(de["ci95"][0] > be),
                                             "point_clears": bool(de["eps"] > be)}
        sens.append({"discount": disc, "eps": de["eps"], "ci95": de["ci95"], "branch": branch(de)["branch"],
                     "cells": cells})
    res["epsilon_sensitivity"] = sens

    # ---- 0.2 k_arb, with a standard error from the daily markout series; declined when SE > KARB_MAX_SE
    per_pool = []
    for pool, g in days.groupby("pool"):
        lvr = g.lvr_usd.sum()
        row = {"pool": name[pool], "class": cls[pool], "lvr_usd": r(lvr, 0), "fees_usd": r(g.fees_usd.sum(), 0),
               "fees_over_lvr": r(g.fees_usd.sum() / lvr) if lvr > 0 else None}
        for h in HORIZONS:
            loss = g[f"loss_h{h}_usd"]
            k = loss.sum() / lvr if lvr > 0 else np.nan
            se = loss.std(ddof=1) * np.sqrt(len(loss)) / lvr if lvr > 0 else np.nan
            row[f"k_arb_h{h}_raw"], row[f"k_arb_h{h}_se"] = r(k), r(se)
            row[f"k_arb_h{h}"] = r(k) if np.isfinite(se) and se <= KARB_MAX_SE else None  # None = declined
        per_pool.append(row)
    per_pool.sort(key=lambda x: (x["class"], x["pool"]))
    kp = pd.DataFrame(per_pool)
    res["k_arb"] = {"caveat": KARB_CAVEAT, "decline_rule": KARB_DECLINE_RULE, "per_pool": per_pool, "median_by_class": {
        c: {f"h{h}": r(g[f"k_arb_h{h}"].dropna().median()) for h in HORIZONS}
           | {f"h{h}_measured_pools": int(g[f"k_arb_h{h}"].notna().sum()) for h in HORIZONS}
           | {"fees_over_lvr": r(g.fees_over_lvr.median()), "n_pools": int(len(g))}
        for c, g in kp.groupby("class")}}

    # ---- band economics (D0.8): does a mandate band have a positive reserve?
    band = pd.read_csv(DATA / "phase0_band_economics.csv", dtype={"x_pct": str})
    kp_idx = {p["pool"]: p for p in per_pool}
    rev = {v: k for k, v in name.items()}
    bres = {"per_pool": [], "by_class": {}}
    for (pool, x), g in band.groupby(["pool", "x_pct"]):
        c = cls[pool]
        own = kp_idx[name[pool]]["k_arb_h25"]
        k = own if own is not None else res["k_arb"]["median_by_class"][c]["h25"]
        lvr, fees = g.lvr_band_usd.sum(), g.fees_band_usd.sum()
        rstar = k * lvr - fees
        row = {"pool": name[pool], "class": c, "x_pct": x, "k_arb_h25": k,
               "k_source": "pool" if own is not None else "class median (pool declined)",
               "notional_usd": int(g.notional_usd.iloc[0]),
               "in_band_share": r(g.in_band_samples.sum() / g.samples.sum()),
               "lvr_band_usd": r(lvr, 0), "fees_band_usd": r(fees, 0),
               "fees_over_lvr": r(fees / lvr), "rstar_usd": r(rstar, 0), "rstar_over_lvr": r(rstar / lvr)}
        for h in HORIZONS:  # sensitivity: the same band under k_arb at every measured horizon
            oh = kp_idx[name[pool]][f"k_arb_h{h}"]
            kh = oh if oh is not None else res["k_arb"]["median_by_class"][c][f"h{h}"]
            row[f"rstar_over_lvr_h{h}"] = r(kh - fees / lvr)
        bres["per_pool"].append(row)
    bres["per_pool"].sort(key=lambda q: (q["class"], q["pool"], q["x_pct"]))
    bp = pd.DataFrame(bres["per_pool"])
    for (c, x), g in bp.groupby(["class", "x_pct"]):
        bres["by_class"].setdefault(c, {})[x] = {
            "pools": int(len(g)), "pools_rstar_positive": int((g.rstar_usd > 0).sum()),
            "median_rstar_over_lvr": r(g.rstar_over_lvr.median()),
            "aggregate_rstar_over_lvr": r(g.rstar_usd.sum() / g.lvr_band_usd.sum()),
            "median_fees_over_lvr": r(g.fees_over_lvr.median()),
            **{f"pools_rstar_positive_h{h}": int((g[f"rstar_over_lvr_h{h}"] > 0).sum()) for h in HORIZONS}}
    bres["pools_rstar_positive"] = {x: int((bp[bp.x_pct == x].rstar_usd > 0).sum()) for x in ("1", "5")}
    bres["pools"] = int(bp.pool.nunique())
    res["band_economics"] = bres

    # ---- 0.4 depth under volatility
    res["vol_depth"] = vol_depth(cls)

    # ---- 0.5 re-centring gap
    res["recentre"] = recentre_gap(cls, name)

    # ---- 0.3 saturation
    s = sat.dropna(subset=["ratio"])
    satres = {"pool_blocks": int(sat.groupby(["pool", "block"]).ngroups), "by_x": {}, "by_class_x10": {}}
    assert satres["pool_blocks"] >= 100, "saturation sample under 100 pool-blocks"
    for lab in XLAB:
        v = s[s.x_pct.astype(str) == lab].ratio
        satres["by_x"][lab] = {"p10": r(v.quantile(.1)), "share_ratio_lt_1": r((v < 1).mean()),
                               "median": r(v.median()), "p75": r(v.quantile(.75)), "p90": r(v.quantile(.9)),
                               "max": r(v.max()), "share_ratio_gt_1_5": r((v > 1.5).mean()),
                               "share_ratio_gt_2": r((v > 2).mean()), "n": int(len(v))}
    for c in ["blue-chip", "mid-cap", "long-tail"]:
        satres["by_class_x10"][c] = {}
        for lab in XLAB:
            v = s[(s.cls == c) & (s.x_pct.astype(str) == lab)].ratio
            satres["by_class_x10"][c][lab] = {"median": r(v.median()), "p90": r(v.quantile(.9)), "p99": r(v.quantile(.99)),
                                              "max": r(v.max()), "share_ratio_lt_1": r((v < 1).mean()),
                                              "share_ratio_gt_1_5": r((v > 1.5).mean()),
                                              "share_ratio_gt_2": r((v > 2).mean()), "n": int(len(v))}
    tz = sat.true_raw.astype(str) == "0"
    satres["zero_depth_rows"] = {"true_zero": int(tz.sum()), "true_zero_naive_positive": int((tz & (sat.naive_usd > 0)).sum()),
                                 "naive_zero_true_positive": int(((sat.naive_usd == 0) & ~tz).sum())}
    # growth of true vs naive from 1% to 10%: naive grows ~linearly (x10); does true depth?
    piv = sat.pivot_table(index=["pool", "block", "side"], columns="x_pct", values=["true_usd", "naive_usd"])
    gt = (piv["true_usd"]["10"] / piv["true_usd"]["1"]).replace([np.inf], np.nan).dropna()
    gn = (piv["naive_usd"]["10"] / piv["naive_usd"]["1"]).replace([np.inf], np.nan).dropna()
    satres["growth_1_to_10pct"] = {"true_median": r(gt.median()), "naive_median": r(gn.median()),
                                   "true_p10": r(gt.quantile(.1)), "true_p25": r(gt.quantile(.25))}
    res["saturation"] = satres

    (DATA / "phase0_results.json").write_text(json.dumps(res, indent=1) + "\n")
    (DATA / "phase0_results.md").write_text(render(res) + "\n")
    print(render(res))


EPS_DISCOUNTS = [1.0, 0.5, 0.33, 0.25]
SKIM_GRID = [0.05, 0.10, 0.20]
SHARE_GRID = [0.10, 0.25, 0.50]
KARB_MAX_SE = 0.5
KARB_DECLINE_RULE = ("POST-HOC (chosen after seeing ratios like 327 and -465 on pools with < $300 of 90-day LVR): a pool's "
                     "k_arb is declined when its standard error, sd(daily markout)·sqrt(days)/LVR, exceeds 0.5. A declined "
                     "pool has no k_arb and falls back to the class prior. See docs/ARCHITECTURE.md D0.7.")
KARB_CAVEAT = ("k_arb is measured against the pool's own future mid, and σ comes from the same pool-price series. It "
               "is the fraction of pool-price variance LPs trade through at a loss, not the fraction of external (CEX) "
               "LVR realised. It understates external LVR wherever the pool price lags the reference market, which "
               "is most likely on stale long-tail books. See docs/ARCHITECTURE.md D0.6(4).")


def render(res: dict) -> str:
    qd = res["quoter_differential"]
    L = ["# Phase 0 results — measured", "",
         f"Generated by `sim/model.py` from committed inputs. Window: 90 days ending {res['window']}. "
         "Uniswap v3, Ethereum mainnet. Every figure here is **measured** from chain data, not modelled.", "",
         f"Replay differential vs QuoterV2: **{qd['walks']} walks, {qd['mismatches']} mismatches**, zero tolerance. "
         f"{qd['walks_with_word_boundary_steps']} walks include word-boundary steps. "
         f"{qd['quoter_declined_zero_depth']} walks were declined by the quoter (the price path crosses zero "
         "liquidity only); each of those is exactly 0 in the replay.", "",
         "Planted mutations, run on the same books against the same quoter results:", "",
         "| Mutation | walks failing | of which boundary walks | still passing (up / down) |", "|---|---|---|---|"]
    for k, v in qd["mutations_on_same_sample"].items():
        L.append(f"| {k} | {v['failing']} | {v['failing_boundary_walks']} | {v['still_passing_up']} / {v['still_passing_down']} |")
    L += ["", "## 0.1 ε — volume elasticity w.r.t. depth", "",
          "The 95% CI the branch reads is the wider of CRV1-t and the wild cluster bootstrap (WCR, Webb, B = 9,999).", "",
          "| Subset | ε (±1%) | CI used | CRV1-t | wild cluster | ε (±5%) | CI used | ε (lagged ±1%) | pools | obs |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in res["epsilon"].items():
        a, b, c = v["depth_1pct"], v["depth_5pct"], v["depth_1pct_lagged"]
        ci = lambda e, key: e.get(key) if e.get(key) is not None else "declined (< 10 pools)"
        L.append(f"| {k} | {a.get('eps')} | {ci(a, 'ci95')} | {ci(a, 'ci95_crv1_t')} | {ci(a, 'ci95_wild_cluster')} | "
                 f"{b.get('eps')} | {ci(b, 'ci95')} | {c.get('eps')} | {a.get('n_pools')} | {a.get('n_obs')} |")
    br = res["epsilon_branch"]
    L += ["", f"**Branch (rule D0.5 as amended by D0.6, non-blue-chip, ±1%): {br['on_depth_1pct']['branch']}**. "
          f"{br['on_depth_1pct']['reason']}. On ±5%: {br['on_depth_5pct']['branch']}.", "", f"Caveat: {br['caveat']}.", "",
          "### Sensitivity: ε discounted for reverse causality (D0.10)", "",
          "The non-blue-chip ±1% estimate and its CI are multiplied by a discount d. Each cell gives whether the "
          "discounted **lower bound** / **point estimate** clears the self-funding break-even ε* = (s + φ)/(1 − φ).", "",
          "| d | ε | CI | branch | " + " | ".join(f"s={sk} φ={ph} (ε*={(sk + ph) / (1 - ph):.3f})" for sk in SKIM_GRID for ph in SHARE_GRID) + " |",
          "|---|---|---|---|" + "---|" * (len(SKIM_GRID) * len(SHARE_GRID))]
    yn = lambda b: "yes" if b else "no"
    for row in res["epsilon_sensitivity"]:
        L.append(f"| {row['discount']} | {row['eps']} | {row['ci95']} | {row['branch']} | " + " | ".join(
            f"{yn(c['lower_bound_clears'])} / {yn(c['point_clears'])}" for c in row["cells"].values()) + " |")
    L += ["",
          "## 0.2 k_arb — realised markout (ex-fees) / theoretical LVR", "", f"Caveat: {res['k_arb']['caveat']}", "",
          f"Decline rule: {res['k_arb']['decline_rule']}", "",
          "| Class | pools | k_arb h=1 | h=25 | h=300 | measured pools (h1/h25/h300) | fees / LVR |", "|---|---|---|---|---|---|---|"]
    for c, v in res["k_arb"]["median_by_class"].items():
        L.append(f"| {c} (median of measured) | {v['n_pools']} | {v['h1']} | {v['h25']} | {v['h300']} | "
                 f"{v['h1_measured_pools']}/{v['h25_measured_pools']}/{v['h300_measured_pools']} | {v['fees_over_lvr']} |")
    fmt = lambda p, h: (f"{p[f'k_arb_h{h}']} ± {p[f'k_arb_h{h}_se']}" if p[f'k_arb_h{h}'] is not None
                        else f"declined ({p[f'k_arb_h{h}_raw']} ± {p[f'k_arb_h{h}_se']})")
    L += ["", "| Pool | class | k_arb h=1 | h=25 | h=300 | fees / LVR | LVR USD | fees USD |", "|---|---|---|---|---|---|---|---|"]
    for p in res["k_arb"]["per_pool"]:
        L.append(f"| {p['pool']} | {p['class']} | {fmt(p, 1)} | {fmt(p, 25)} | {fmt(p, 300)} | {p['fees_over_lvr']} | "
                 f"{p['lvr_usd']:,.0f} | {p['fees_usd']:,.0f} |")
    be = res["band_economics"]
    L += ["", "## Band economics — does a mandate band have a positive reserve?", "",
          "Synthetic mandate band at ±x around spot, re-centred daily, sized so the band alone supplies notional D of depth "
          "to +x (D0.8). R*_band = k_arb(h=25)·LVR_band − fees_band over the window. Price-taking: the sign does not "
          "depend on D.", "",
          f"**Pools with R*_band > 0: {be['pools_rstar_positive']['1']} of {be['pools']} at ±1%, "
          f"{be['pools_rstar_positive']['5']} of {be['pools']} at ±5%.**", "",
          "| Class | ±x | pools | R* > 0 (h=25) | median R*/LVR | aggregate R*/LVR | median fees/LVR | R* > 0 under k_arb h=1 / h=300 |",
          "|---|---|---|---|---|---|---|---|"]
    for c, xs in be["by_class"].items():
        for x, v in xs.items():
            L.append(f"| {c} | {x}% | {v['pools']} | {v['pools_rstar_positive']} | {v['median_rstar_over_lvr']} | "
                     f"{v['aggregate_rstar_over_lvr']} | {v['median_fees_over_lvr']} | "
                     f"{v['pools_rstar_positive_h1']} / {v['pools_rstar_positive_h300']} |")
    L += ["", "The headline horizon is h=25 (pre-registered, D0.5). The last column shows the same bands under k_arb at "
          "h=1 and h=300. No horizon makes R* positive on a majority of mid-cap or long-tail pools. The blue-chip positive count "
          "is itself fragile: it holds at h=25 but falls to 0–2 of 8 at h=1 and h=300 (D0.8)."]
    L += ["", "| Pool | class | ±x | k_arb | k source | in band | LVR_band USD | fees_band USD | fees/LVR | R* USD | R*/LVR |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in be["per_pool"]:
        L.append(f"| {p['pool']} | {p['class']} | {p['x_pct']}% | {p['k_arb_h25']} | {p['k_source']} | {p['in_band_share']} | "
                 f"{p['lvr_band_usd']:,.0f} | {p['fees_band_usd']:,.0f} | {p['fees_over_lvr']} | {p['rstar_usd']:,.0f} | {p['rstar_over_lvr']} |")
    s = res["saturation"]
    L += ["", "## 0.3 Saturation — naive depth / true depth", "",
          "**Naive depth is an excellent point estimate and a worthless guarantee.**", "",
          "At the median the naive figure is right: the ratio is about 1 at every width, and exactly 1.000 at every width "
          "on long-tail pools. A contract that pays on depth is not priced on the median. It is priced on the tail, and in "
          "the tail the naive figure fails in **both** directions: it overstates by more than 1.5× in a material share of "
          "books, without bound, and it *understates* in roughly a third of them. Neither failure is visible from the "
          "naive number itself.", "",
          f"{s['pool_blocks']} pool-blocks (one per pool per day), both sides.", "",
          "| ±x | p10 | median | p75 | p90 | max | share < 1 | share > 1.5 | share > 2 | n |", "|---|---|---|---|---|---|---|---|---|---|"]
    for lab, v in s["by_x"].items():
        L.append(f"| {lab}% | {v['p10']} | {v['median']} | {v['p75']} | {v['p90']} | {v['max']} | {v['share_ratio_lt_1']} | "
                 f"{v['share_ratio_gt_1_5']} | {v['share_ratio_gt_2']} | {v['n']} |")
    L += ["", "By class:", "",
          "| Class | ±x | median | p90 | p99 | max | share < 1 (understates) | share > 1.5 | share > 2 | n |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for c, xs in s["by_class_x10"].items():
        for lab in XLAB:
            v = xs[lab]
            L.append(f"| {c} | {lab}% | {v['median']} | {v['p90']} | {v['p99']} | {v['max']} | {v['share_ratio_lt_1']} | "
                     f"{v['share_ratio_gt_1_5']} | {v['share_ratio_gt_2']} | {v['n']} |")
    z = s["zero_depth_rows"]
    g = s["growth_1_to_10pct"]
    L += ["", f"Zero-depth books: {z['true_zero']} rows have true depth exactly 0, and "
          f"{z['true_zero_naive_positive']} of those have positive naive depth: true depth is 0 only when the whole path, spot included, has no liquidity, so naive is "
          f"0 as well. The reverse, naive 0 with true depth positive (empty at spot, liquidity beyond), occurs in "
          f"{z['naive_zero_true_positive']} rows. The 30 walks QuoterV2 declined in the differential are zero-liquidity "
          "paths of the first kind.", "",
          f"Depth growth from ±1% to ±10%: naive median ×{g['naive_median']}, true median ×{g['true_median']} "
          f"(true p25 ×{g['true_p25']}, p10 ×{g['true_p10']}).", "",
          "The pre-measurement prediction that the gap would be largest on long-tail books is falsified. See "
          "docs/ARCHITECTURE.md D0.9."]
    vd = res["vol_depth"]
    L += ["", "## 0.4 Depth under volatility — does depth leave when it is needed?", "",
          "Effective liquidity L_eff = D/Δ√p for the exact walk to ±x. It is price-neutral: it moves only when the book's "
          "shape around spot moves. Depth is relative to the median of the previous 24 hours. A pool's top-decile hours "
          "are its 10% largest absolute hourly log returns. Active = mint/burn during the hour; passive = price moving "
          "through a fixed book. Both are ratio-of-sums shares of baseline (Σ change / Σ baseline). See D0.11.", "",
          f"Thresholds: {vd['thresholds_note']}", "",
          "| Class | ±x | median depth vs baseline, top decile | rest | p10 top | p10 rest | active, top | rest | "
          "passive, top | rest |", "|---|---|---|---|---|---|---|---|---|---|"]
    for c, xs in vd["by_class"].items():
        for x, v in xs.items():
            L.append(f"| {c} | {x}% | {v['median_rel_top']} | {v['median_rel_rest']} | {v['p10_rel_top']} | "
                     f"{v['p10_rel_rest']} | {v['active_top']} | {v['active_rest']} | {v['passive_top']} | {v['passive_rest']} |")
    pl = vd["pools_top_decile_lower"]
    L += ["", f"Pools whose median depth is lower in top-decile hours than in the rest: {pl['1']['pools']} of "
          f"{pl['1']['of']} at ±1%, {pl['5']['pools']} of {pl['5']['of']} at ±5%.", "",
          "Event study: top-decile hours (**vol**) against a placebo of calm hours, |r| ≤ pool median (**calm**). Both are "
          "declustered 24 h apart. Trough is the minimum over the next 24 h; recovery is back to 95% of baseline. The trough "
          "is negative for any noisy series, so only the vol − calm difference carries information.", "",
          "| Class | events | ±x | n | median trough | p10 trough | trough < −5% | trough < −20% | median recovery h (given −5%) | "
          "unrecovered at 72 h |", "|---|---|---|---|---|---|---|---|---|---|"]
    for c, kinds in vd["events"].items():
        for kind in ("vol", "calm"):
            for x, v in kinds[kind].items():
                L.append(f"| {c} | {kind} | {x}% | {v['events']} | {v['median_trough']} | {v['p10_trough']} | "
                         f"{v['share_trough_below_minus_5pct']} | {v['share_trough_below_minus_20pct']} | "
                         f"{v['median_recovery_hours_given_drawdown']} | {v['share_unrecovered_72h_given_drawdown']} |")
    L += ["", "Tail hours (depth more than 20% below baseline at ±1%): how much of that hour's own change was mint/burn?", "",
          "| Class | hours | n | median hour change | median active share of the hour's loss | hours where burn loss exceeded drift loss |",
          "|---|---|---|---|---|---|"]
    for c, kinds in vd["tail_hours"].items():
        for kind, v in kinds.items():
            L.append(f"| {c} | {kind} | {v['hours']} | {v['median_hour_change']} | {v['median_active_share_of_hour_change']} | "
                     f"{v['share_hours_active_exceeds_passive_loss']} |")
    st = vd["stress"].values()
    L += ["", f"Stress in the window: the largest absolute hourly log return across pools is "
          f"{max(v['max_abs_hourly_return'] for v in st)}, and the worst peak-to-trough is "
          f"{min(v['worst_drawdown'] for v in st)}. Per-pool figures are under vol_depth.stress in "
          "phase0_results.json."]
    rc = res["recentre"]
    L += ["", "## 0.5 The re-centring gap — is staying centred worth what it costs?", "",
          "Pre-registered in D0.12, amended before the full run in D0.13. Curator **P** never re-centres. **R_N** re-centres "
          "N blocks after spot leaves its band, rebalancing through the pool's real book (exact v3 math). Gap = R − P as a "
          "% of capital over 90 days. benefit = extra fees; cost = rebalance (pool fee + impact) + gas − market (the "
          "crystallised move against holding the option). Headline: R_300 vs P, ±5%, the 15 target pools.", ""]
    if rc["declined_pools"]:
        L += [f"Declined (median active liquidity 0): {', '.join(rc['declined_pools'])}.", ""]
    L += ["| Run | gap basis | median gap % | gap > 0 | gap < 0 | median benefit % | median rebalance % | median market % | "
          "median abs(benefit) % | median abs(cost) % | bands larger than pool | verdict |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for sizing, v in rc["headline"].items():
        L.append(f"| {sizing} | {v['gap_basis']} | {v['median_gap_pct']} | {v['pools_gap_positive']} of {v['target_pools']} | "
                 f"{v['pools_gap_negative']} | {v['median_benefit_pct']} | {v['median_rebalance_pct']} | {v['median_market_pct']} | "
                 f"{v['median_abs_benefit_pct']} | {v['median_abs_cost_pct']} | {v['pools_band_larger_than_pool']} | **{v['verdict']}** |")
    L += ["", "The registered run is outside its own validity wherever the band is larger than the pool (D0.13). "
          "Classification is read off the **small** run (band = 1% of the pool's median active liquidity, gas excluded).", "",
          "Small run, R_300 vs P at ±5%, per pool (registered gap shown for comparison):", "",
          "| Pool | class | re-centres | skipped | benefit % | rebalance % | market % | gap ex-gas % | gas USD total | "
          "capital where gas = 0.5% | registered gap % | registered band/pool | pool scale USD (±5% band = 100% of median active L) |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for q in rc["per_pool_headline"]:
        L.append(f"| {q['pool']} | {q['class']} | {q['recentres']} | {q['skipped']} | {q['benefit_pct']} | {q['rebalance_pct']} | "
                 f"{q['market_pct']} | {q['gap_excl_gas_pct']} | {q['gas_usd_total']} | {q['capital_where_gas_is_half_pct_usd']:,.0f} | "
                 f"{q['registered_gap_pct']} | {q['registered_size_ratio']} | {q['pool_scale_usd']:,.0f} |")
    L += ["", "Venue-independent bound: the median target-pool gap if rebalancing cost nothing at all is "
          f"{rc['headline']['small']['median_gap_if_rebalance_free_pct']}% "
          f"({rc['headline']['small']['pools_positive_if_rebalance_free']} of {rc['headline']['small']['target_pools']} pools "
          "positive). No choice of swap venue can do better than this.", "",
          "Control group: owners that do re-centre (D0.12 rule: non-NPM owner, burn and re-mint to a different range within "
          "10 blocks, at least 5 times). Latency = blocks from spot leaving the burned range to the re-mint.", "",
          "| Class | pools with managed owners | owners | re-centre events | median owner latency (blocks) | median owner share of "
          "active L | owner fee yield / day | sim P yield / day | sim R_300 yield / day | sim gap % where managed | where not |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c, v in rc["managed"].items():
        L.append(f"| {c} | {v['pools_with_managed_owners']} of {v['pools']} | {v['managed_owners']} | {v['recentre_events']} | "
                 f"{v['median_owner_latency_blocks']} | {v['median_owner_active_share']} | {v['median_owner_fee_yield_per_day']} | "
                 f"{v['sim_P_fee_yield_per_day']} | {v['sim_R300_fee_yield_per_day']} | {v['median_sim_gap_pct_where_managed']} | "
                 f"{v['median_sim_gap_pct_where_not_managed']} |")
    L += ["", "Sensitivity, small run: median gap % by class, band and N (pools with gap > 0 in brackets):", "",
          "| Class | ±x | N=25 | N=300 | N=1800 | N=7200 |", "|---|---|---|---|---|---|"]
    for c, xs in rc["grid"]["small"].items():
        for x, ns in xs.items():
            L.append(f"| {c} | {x}% | " + " | ".join(f"{ns[n]['median_gap_pct']} ({ns[n]['pools_positive']}/{ns[n]['pools']})"
                                                       for n in ("25", "300", "1800", "7200")) + " |")
    return "\n".join(L)


if __name__ == "__main__":
    main()
