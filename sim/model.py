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
    return {"eps": r(m.params["ld"]), "se": r(m.bse["ld"]), "ci95": [r(lo), r(hi)], "n_obs": int(len(d)),
            "n_pools": int(d.pool.nunique())}


def branch(e: dict) -> dict:
    """docs/ARCHITECTURE.md D0.5: CI straddling a threshold takes the more conservative branch."""
    if e.get("eps") is None:
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
    days = pd.read_csv(DATA / "phase0_pool_days.csv")
    sat = pd.read_csv(DATA / "phase0_saturation_books.csv")
    cls = classify(pools, days)
    name = {p["pool"]: f'{p["base"]}/{p["quote"]} {p["fee"] / 1e4:g}%' for p in pools}
    days["cls"] = days.pool.map(cls)
    sat["cls"] = sat.pool.map(cls)

    res = {"window": json.loads((DATA / "phase0_window.json").read_text())["end_utc"],
           "classes": {name[k]: v for k, v in sorted(cls.items(), key=lambda kv: (kv[1], name[kv[0]]))},
           "quoter_differential": {k: qd[k] for k in ("walks", "mismatches", "walks_with_word_boundary_steps")}}

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

    # ---- 0.2 k_arb
    per_pool = []
    for pool, g in days.groupby("pool"):
        lvr = g.lvr_usd.sum()
        row = {"pool": name[pool], "class": cls[pool], "lvr_usd": r(lvr, 0), "fees_usd": r(g.fees_usd.sum(), 0),
               "fees_over_lvr": r(g.fees_usd.sum() / lvr) if lvr > 0 else None}
        for h in HORIZONS:
            row[f"k_arb_h{h}"] = r(g[f"loss_h{h}_usd"].sum() / lvr) if lvr > 0 else None
        per_pool.append(row)
    per_pool.sort(key=lambda x: (x["class"], x["pool"]))
    kp = pd.DataFrame(per_pool)
    res["k_arb"] = {"per_pool": per_pool, "median_by_class": {
        c: {f"h{h}": r(g[f"k_arb_h{h}"].median()) for h in HORIZONS} | {"fees_over_lvr": r(g.fees_over_lvr.median()),
                                                                       "n_pools": int(len(g))}
        for c, g in kp.groupby("class")}}

    # ---- 0.3 saturation
    s = sat.dropna(subset=["ratio"])
    satres = {"pool_blocks": int(sat.groupby(["pool", "block"]).ngroups), "by_x": {}, "by_class_x10": {}}
    assert satres["pool_blocks"] >= 100, "saturation sample under 100 pool-blocks"
    for lab in XLAB:
        v = s[s.x_pct.astype(str) == lab].ratio
        satres["by_x"][lab] = {"median": r(v.median()), "p75": r(v.quantile(.75)), "p90": r(v.quantile(.9)),
                               "max": r(v.max()), "share_ratio_gt_1_5": r((v > 1.5).mean()),
                               "share_ratio_gt_2": r((v > 2).mean()), "n": int(len(v))}
    for c in ["blue-chip", "mid-cap", "long-tail"]:
        satres["by_class_x10"][c] = {}
        for lab in XLAB:
            v = s[(s.cls == c) & (s.x_pct.astype(str) == lab)].ratio
            satres["by_class_x10"][c][lab] = {"median": r(v.median()), "p90": r(v.quantile(.9)), "n": int(len(v))}
    # growth of true vs naive from 1% to 10%: naive grows ~linearly (x10); does true depth?
    piv = sat.pivot_table(index=["pool", "block", "side"], columns="x_pct", values=["true_usd", "naive_usd"])
    gt = (piv["true_usd"][10] / piv["true_usd"][1]).replace([np.inf], np.nan).dropna()
    gn = (piv["naive_usd"][10] / piv["naive_usd"][1]).replace([np.inf], np.nan).dropna()
    satres["growth_1_to_10pct"] = {"true_median": r(gt.median()), "naive_median": r(gn.median()),
                                   "true_p10": r(gt.quantile(.1)), "true_p25": r(gt.quantile(.25))}
    res["saturation"] = satres

    (DATA / "phase0_results.json").write_text(json.dumps(res, indent=1) + "\n")
    (DATA / "phase0_results.md").write_text(render(res) + "\n")
    print(render(res))


def render(res: dict) -> str:
    L = ["# Phase 0 results — measured", "",
         f"Generated by `sim/model.py` from committed inputs. Window: 90 days ending {res['window']}. "
         "Uniswap v3, Ethereum mainnet. Every figure here is **measured** from chain data, not modelled.", "",
         f"Replay differential vs QuoterV2: **{res['quoter_differential']['walks']} walks, "
         f"{res['quoter_differential']['mismatches']} mismatches**, zero tolerance "
         f"({res['quoter_differential']['walks_with_word_boundary_steps']} walks include word-boundary steps).", "",
         "## 0.1 ε — volume elasticity w.r.t. depth", "",
         "| Subset | ε (±1% depth) | 95% CI | ε (±5%) | 95% CI | ε (lagged ±1%) | pools | obs |", "|---|---|---|---|---|---|---|---|"]
    for k, v in res["epsilon"].items():
        a, b, c = v["depth_1pct"], v["depth_5pct"], v["depth_1pct_lagged"]
        L.append(f"| {k} | {a.get('eps')} | {a.get('ci95')} | {b.get('eps')} | {b.get('ci95')} | {c.get('eps')} | {a.get('n_pools')} | {a.get('n_obs')} |")
    br = res["epsilon_branch"]
    L += ["", f"**Branch (rule D0.5, non-blue-chip, ±1%): {br['on_depth_1pct']['branch']}** — {br['on_depth_1pct']['reason']}. "
          f"On ±5%: {br['on_depth_5pct']['branch']}.", "", f"Caveat: {br['caveat']}.", "",
          "## 0.2 k_arb — realised markout (ex-fees) / theoretical LVR", "",
          "| Class | pools | k_arb h=1 | h=25 | h=300 | fees / LVR |", "|---|---|---|---|---|---|"]
    for c, v in res["k_arb"]["median_by_class"].items():
        L.append(f"| {c} (median) | {v['n_pools']} | {v['h1']} | {v['h25']} | {v['h300']} | {v['fees_over_lvr']} |")
    L += ["", "| Pool | class | k_arb h=1 | h=25 | h=300 | fees / LVR | LVR USD | fees USD |", "|---|---|---|---|---|---|---|---|"]
    for p in res["k_arb"]["per_pool"]:
        L.append(f"| {p['pool']} | {p['class']} | {p['k_arb_h1']} | {p['k_arb_h25']} | {p['k_arb_h300']} | {p['fees_over_lvr']} | {p['lvr_usd']:,.0f} | {p['fees_usd']:,.0f} |")
    s = res["saturation"]
    L += ["", "## 0.3 Saturation — naive depth / true depth", "",
          f"{s['pool_blocks']} pool-blocks (one per pool per day), both sides.", "",
          "| ±x | median | p75 | p90 | max | share > 1.5 | share > 2 | n |", "|---|---|---|---|---|---|---|---|"]
    for lab, v in s["by_x"].items():
        L.append(f"| {lab}% | {v['median']} | {v['p75']} | {v['p90']} | {v['max']} | {v['share_ratio_gt_1_5']} | {v['share_ratio_gt_2']} | {v['n']} |")
    L += ["", "Median ratio by class:", "", "| Class | " + " | ".join(f"±{x}%" for x in XLAB) + " |", "|---|" + "---|" * len(XLAB)]
    for c, v in s["by_class_x10"].items():
        L.append(f"| {c} | " + " | ".join(str(v[x]["median"]) for x in XLAB) + " |")
    g = s["growth_1_to_10pct"]
    L += ["", f"Depth growth from ±1% to ±10%: naive median ×{g['naive_median']}, true median ×{g['true_median']} "
          f"(true p25 ×{g['true_p25']}, p10 ×{g['true_p10']})."]
    return "\n".join(L)


if __name__ == "__main__":
    main()
