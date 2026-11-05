"""Steering transfer: do SAE-feature steering interventions survive quantization?

For a sample of live features, add a vector along the feature's decoder
direction to the residual stream at the SAE hook, in both the reference and
the quantized model, and compare how next-token log-probs move.

Definitions (BOS, position 0, is excluded everywhere):

- live features: fire (> 0) on at least `min_count` tokens of the reference
  model on `tokens`. `n_features` of them are sampled uniformly per hook with a
  torch.Generator seeded by `seed` (fewer if fewer are live).
- steering vector: alpha * r * d_hat, with d_hat = W_dec[f] / ||W_dec[f]|| and
  r = mean L2 norm of the reference residual stream at that hook. So alpha is
  in units of the typical residual norm, comparable across layers and models.
  It is added at every position except BOS, via run_with_hooks (permanent
  hooks on the quantized model, e.g. activation quantization, stay in place).
- per token: delta_ref = logp_ref_steered - logp_ref_clean and
  delta_q = logp_q_steered - logp_q_clean (log_softmax over the vocab).

Metrics per (hook, feature, alpha), averaged over tokens:

- cos_mean: cosine(delta_ref, delta_q). 1 means steering moves the quantized
  model's predictions the same way it moves the reference.
- kl_steered: KL(p_ref_steered || p_q_steered).
- kl_clean: KL(p_ref_clean || p_q_clean), the unsteered baseline.
- delta_norm_ref / delta_norm_q: L2 norm of the deltas.
- effect_ratio: delta_norm_q / delta_norm_ref (> 1: steering hits harder
  after quantization, < 1: weaker).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402


def _logp(logits: torch.Tensor) -> torch.Tensor:
    """[batch, pos, vocab] logits -> [tokens, vocab] float32 log-probs, BOS dropped."""
    return logits[:, 1:].float().log_softmax(-1).reshape(-1, logits.shape[-1])


def _kl(lp: torch.Tensor, lq: torch.Tensor) -> torch.Tensor:
    return (lp.exp() * (lp - lq)).sum(-1)


@torch.no_grad()
def _live_and_norms(model, saes, tokens, batch_size, min_count, device):
    """Per hook: firing counts per feature and mean residual norm (non-BOS)."""
    hooks = list(saes)
    counts = {h: 0 for h in hooks}
    norm_sum = {h: 0.0 for h in hooks}
    n = 0
    for i in range(0, tokens.shape[0], batch_size):
        _, cache = model.run_with_cache(tokens[i:i + batch_size].to(device), names_filter=hooks)
        for h in hooks:
            x = cache[h][:, 1:].reshape(-1, cache[h].shape[-1])
            counts[h] = counts[h] + (saes[h].encode(x) > 0).sum(0)
            norm_sum[h] += x.float().norm(dim=-1).sum().item()
        n += x.shape[0]
    live = {h: torch.nonzero(counts[h] >= min_count).flatten().cpu() for h in hooks}
    return live, {h: norm_sum[h] / max(n, 1) for h in hooks}


def _steer_hook(v: torch.Tensor):
    def hook(x, hook):
        x = x.clone()
        x[:, 1:] += v.to(x.dtype)
        return x
    return hook


@torch.no_grad()
def run_steering(model, qmodel, saes: dict[str, object], tokens: torch.Tensor,
                 n_features: int = 32, alphas: tuple[float, ...] = (0.5,), seed: int = 0,
                 batch_size: int = 4, min_count: int = 5, device: str = "cpu",
                 log=print) -> list[dict]:
    """Compare SAE-feature steering effects in `model` vs `qmodel`.

    Args:
        saes: mapping from hook name (e.g. "blocks.6.hook_resid_pre") to SAE.
        tokens: [n_seqs, ctx] token ids starting with BOS.
    Returns one row per (hook, feature, alpha) with keys hook, feature, alpha,
    r_norm, cos_mean, kl_steered, kl_clean, delta_norm_ref, delta_norm_q,
    effect_ratio (see module docstring).
    """
    model.eval()
    qmodel.eval()
    hooks = list(saes)
    live, r_norm = _live_and_norms(model, saes, tokens, batch_size, min_count, device)

    g = torch.Generator().manual_seed(seed)
    feats = {}
    for h in hooks:
        perm = torch.randperm(len(live[h]), generator=g)[:n_features]
        feats[h] = live[h][perm].sort().values.tolist()
        log(f"{h}: {len(live[h])} live features, steering {len(feats[h])}, r={r_norm[h]:.2f}")

    vecs = {}
    for h in hooks:
        for f in feats[h]:
            d = saes[h].W_dec[f].detach().float().to(device)
            d_hat = d / d.norm()
            for a in alphas:
                vecs[(h, f, a)] = a * r_norm[h] * d_hat

    # running sums: cos, kl_steered, delta_norm_ref, delta_norm_q
    sums = {k: [0.0, 0.0, 0.0, 0.0] for k in vecs}
    kl_clean_sum = 0.0
    n_tok = 0
    for i in range(0, tokens.shape[0], batch_size):
        batch = tokens[i:i + batch_size].to(device)
        lr_c = _logp(model(batch))
        lq_c = _logp(qmodel(batch))
        kl_clean_sum += _kl(lr_c, lq_c).double().sum().item()
        n_tok += lr_c.shape[0]
        for (h, f, a), v in vecs.items():
            fwd = [(h, _steer_hook(v))]
            lr_s = _logp(model.run_with_hooks(batch, fwd_hooks=fwd))
            lq_s = _logp(qmodel.run_with_hooks(batch, fwd_hooks=fwd))
            d_r, d_q = lr_s - lr_c, lq_s - lq_c
            s = sums[(h, f, a)]
            s[0] += F.cosine_similarity(d_r, d_q, dim=-1).double().sum().item()
            s[1] += _kl(lr_s, lq_s).double().sum().item()
            s[2] += d_r.norm(dim=-1).double().sum().item()
            s[3] += d_q.norm(dim=-1).double().sum().item()
            del lr_s, lq_s, d_r, d_q

    n = max(n_tok, 1)
    rows = []
    for (h, f, a), s in sums.items():
        dn_r, dn_q = s[2] / n, s[3] / n
        rows.append({"hook": h, "feature": f, "alpha": a, "r_norm": r_norm[h],
                     "cos_mean": s[0] / n, "kl_steered": s[1] / n, "kl_clean": kl_clean_sum / n,
                     "delta_norm_ref": dn_r, "delta_norm_q": dn_q,
                     "effect_ratio": dn_q / dn_r if dn_r > 0 else float("nan")})
    for h in hooks:
        for a in alphas:
            cs = torch.tensor([r["cos_mean"] for r in rows if r["hook"] == h and r["alpha"] == a])
            if len(cs):
                log(f"{h} alpha={a}: median cos={cs.median().item():.3f}")
    return rows


def plot_steering(df, path: str):
    """Median steering cosine and steered KL vs bits, one line per hook
    (per hook and alpha / scheme when several are present)."""
    keys = ["hook"] + [c for c in ("alpha", "scheme") if c in df and df[c].nunique() > 1]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.4))
    for i, (key, g) in enumerate(df.groupby(keys)):
        key = key if isinstance(key, tuple) else (key,)
        label = " ".join([key[0].replace(".hook_resid_pre", "")] + [f"{k}={v}" for k, v in zip(keys[1:], key[1:])])
        med = g.groupby("bits")[["cos_mean", "kl_steered", "kl_clean"]].median().sort_index(ascending=False)
        color = f"C{i % 10}"
        axes[0].plot(med.index, med["cos_mean"], marker="o", color=color, label=label)
        axes[1].plot(med.index, med["kl_steered"], marker="o", color=color, label=label)
        axes[1].plot(med.index, med["kl_clean"], ls="--", color=color, alpha=0.7)
    axes[0].set_title("Median steering cosine (ref vs quantized)", fontsize=10)
    axes[1].set_title("Median KL steered (dashed: clean)", fontsize=10)
    for ax in axes:
        ax.set_xlabel("weight bits")
        ax.invert_xaxis()
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
