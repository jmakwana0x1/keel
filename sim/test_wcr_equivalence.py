"""The affine-in-b0 WCR p-value must equal a direct per-draw re-estimation, exactly up to float error."""
import numpy as np
import model

rng = np.random.default_rng(7)
G, T = 23, 40
g = np.repeat(np.arange(G), T)
Z = np.column_stack([rng.normal(size=G * T), np.eye(G)[g]])
x = rng.normal(size=G * T) + rng.normal(size=G)[g]
y = 0.3 * x + rng.normal(size=G)[g] + rng.normal(size=G * T)
model.BOOT_B = 499
_, _, (lo, hi) = model._wcr_ci(x, y, Z, g)

# direct implementation: full re-estimation per draw, written independently
Q, _ = np.linalg.qr(Z); M = lambda v: v - Q @ (Q.T @ v)
xt, yt = M(x), M(y); sxx = xt @ xt; N, K = len(y), np.linalg.matrix_rank(Z) + 1; c = G / (G - 1) * (N - 1) / (N - K)
W = model.WEBB[np.random.default_rng(model.BOOT_SEED).integers(0, 6, size=(G, model.BOOT_B))]
def se(b, yy):
    e = yy - b * xt
    return np.sqrt(c * sum((xt[g == k] @ e[g == k]) ** 2 for k in range(G)) / sxx ** 2)
bhat = xt @ yt / sxx
def p_direct(b0):
    t = (bhat - b0) / se(bhat, yt); n = 0
    for j in range(model.BOOT_B):
        ys = b0 * xt + M((yt - b0 * xt) * W[g, j])
        bs = xt @ ys / sxx
        n += abs((bs - b0) / se(bs, ys)) >= abs(t)
    return (n + 1) / (model.BOOT_B + 1)
for b0 in (lo - 1e-6, lo + 1e-6, hi - 1e-6, hi + 1e-6):
    print(f"b0={b0:+.6f}  direct p={p_direct(b0):.4f}")
print("CI from vectorised:", round(lo, 6), round(hi, 6), "(direct p should cross 0.05 at each bound)")
