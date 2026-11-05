"""Per-feature fragility: which SAE features break first under quantization?

`run_experiment(..., per_feature_dir=d)` saves, per (hook, bits), a file
d/f"{hook}_{bits}bit.pt" holding per-feature freq_ref, freq_q, corr and
mean_active_ref plus n_tokens. This module ranks the alive features of one bit
width by corr (the Pearson correlation of a feature's activations across
tokens, reference vs quantized), pulls the lowest ("fragile") and highest
("robust") ones, and finds the reference-model contexts where each fires most.
It then asks whether fragility tracks simple feature properties, using Spearman
rank correlation so the answer does not depend on scale.

A feature is alive when it fires on at least `min_count` tokens in the
reference model and its corr is finite.

fragile_features.csv, one row per selected feature:
- hook, layer, bits: where and at which bit width it was measured.
- kind: "fragile" (lowest corr) or "robust" (highest corr).
- rank: 0-based position within its kind (0 = most fragile / most robust).
- feature: SAE feature index.
- corr: reference vs quantized activation correlation.
- freq_ref / freq_q: fraction of tokens the feature fires on, per model.
- mean_active_ref: mean reference activation on tokens where it fires.
- dec_norm: L2 norm of the feature's decoder row, sae.W_dec[feature].
- contexts: top reference contexts, joined with " | ". Each is the `window`
  tokens before the token, then the token itself marked «like this».
- neuronpedia: dashboard link, empty when no template is given.

fragility_summary.csv, one row per hook:
- hook, layer, bits, n_alive, median_corr.
- rho_log10_freq_ref, rho_mean_active_ref, rho_dec_norm: Spearman rank
  correlation of corr with that property over alive features. Positive means
  features with a larger value are more robust.

fragility_scatter.png: one row per hook, corr against each of the three
properties, rho in the titles.
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402

from .quantize import block_of  # noqa: E402

PROPERTIES = [("log10_freq_ref", "log10 freq_ref"), ("mean_active_ref", "mean_active_ref"),
              ("dec_norm", "dec_norm")]


def feature_table(pf: dict, sae, min_count: int = 5) -> pd.DataFrame:
    """One row per alive feature: feature, corr, freq_ref, freq_q, mean_active_ref, dec_norm."""
    corr = pf["corr"].float()
    counts = (pf["freq_ref"].double() * pf["n_tokens"]).round()
    alive = (counts >= min_count) & torch.isfinite(corr)
    idx = alive.nonzero().squeeze(1)
    dec_norm = sae.W_dec.detach().float().norm(dim=1).cpu()
    return pd.DataFrame({
        "feature": idx.numpy(),
        "corr": corr[idx].numpy(),
        "freq_ref": pf["freq_ref"][idx].float().numpy(),
        "freq_q": pf["freq_q"][idx].float().numpy(),
        "mean_active_ref": pf["mean_active_ref"][idx].float().numpy(),
        "dec_norm": dec_norm[idx].numpy(),
    })


def _snippet(model, ids: list[int], window: int, pos: int) -> str:
    before, tok = ids[max(0, pos - window):pos], ids[pos]
    if getattr(model, "tokenizer", None) is None:
        s = " ".join(map(str, before)) + f" «{tok}»"
    else:
        s = model.to_string(torch.tensor(before)) + f"«{model.to_string(torch.tensor([tok]))}»"
    return s.replace("\n", "\\n").strip()


@torch.no_grad()
def top_contexts(model, sae, hook: str, tokens: torch.Tensor, features: list[int], k: int = 3,
                 window: int = 8, batch_size: int = 8, device: str = "cpu") -> dict[int, list[str]]:
    """Top-k reference-model contexts per feature, strongest first.

    Streams over `tokens` [n_seqs, ctx], skipping the BOS position, and keeps
    only the selected features' activations. Positions where a feature does
    not fire are never returned, so a list may be shorter than k.
    """
    if not features:
        return {}
    feats = torch.tensor(features, device=device)
    ctx = tokens.shape[1]
    block = block_of(hook)
    stop = None if block is None else block + 1  # resid_pre of block L needs blocks[:L+1]
    best_v = torch.full((k, len(features)), -float("inf"), device=device)
    best_i = torch.zeros((k, len(features)), dtype=torch.long, device=device)
    for i in range(0, tokens.shape[0], batch_size):
        batch = tokens[i:i + batch_size].to(device)
        _, cache = model.run_with_cache(batch, names_filter=[hook], stop_at_layer=stop)
        x = cache[hook][:, 1:].reshape(-1, cache[hook].shape[-1])
        acts = sae.encode(x)[:, feats]
        # flat index seq * ctx + pos over the whole token tensor
        pos = torch.arange(1, ctx, device=device).repeat(batch.shape[0])
        seq = torch.arange(i, i + batch.shape[0], device=device).repeat_interleave(ctx - 1)
        flat = (seq * ctx + pos).unsqueeze(1).expand_as(acts)
        v = torch.cat([best_v, acts.float()])
        top = v.topk(min(k, v.shape[0]), dim=0)
        best_v, best_i = top.values, torch.cat([best_i, flat]).gather(0, top.indices)
    rows = tokens.tolist()
    out = {}
    for j, f in enumerate(features):
        out[f] = [_snippet(model, rows[n // ctx], window, n % ctx)
                  for v, n in zip(best_v[:, j].tolist(), best_i[:, j].tolist()) if v > 0]
    return out


def _spearman(df: pd.DataFrame, col: str) -> float:
    # Pearson on average ranks: Series.corr(method="spearman") would need scipy
    return float(df["corr"].rank().corr(df[col].rank())) if len(df) > 1 else float("nan")


def analyze_fragility(model, saes: dict[str, object], tokens: torch.Tensor, per_feature_dir: str,
                      bits: int, out_dir: str, layers: dict[str, int],
                      neuronpedia_template: str | None = "https://www.neuronpedia.org/gpt2-small/{layer}-res-jb/{feature}",
                      n: int = 50, n_contexts: int = 3, window: int = 8, batch_size: int = 8,
                      min_count: int = 5, device: str = "cpu") -> pd.DataFrame:
    """Rank features by fragility at `bits` and write fragile_features.csv,
    fragility_summary.csv and fragility_scatter.png to `out_dir`.

    Picks up to `n` fragile and `n` robust features per hook; with fewer than
    2n alive features they are split between the two kinds without overlap.
    Returns the fragile_features table.
    """
    os.makedirs(out_dir, exist_ok=True)
    picked, summary, tables = [], [], {}
    for hook, sae in saes.items():
        layer = layers[hook]
        pf = torch.load(os.path.join(per_feature_dir, f"{hook}_{bits}bit.pt"))
        t = feature_table(pf, sae, min_count).sort_values("corr", kind="stable").reset_index(drop=True)
        t["log10_freq_ref"] = np.log10(t["freq_ref"])
        tables[hook] = t

        n_frag = min(n, (len(t) + 1) // 2)
        n_rob = min(n, len(t) - n_frag)
        sel = pd.concat([t.head(n_frag).assign(kind="fragile", rank=range(n_frag)),
                         t.tail(n_rob).iloc[::-1].assign(kind="robust", rank=range(n_rob))])
        ctxs = top_contexts(model, sae, hook, tokens, sel["feature"].tolist(), n_contexts,
                            window, batch_size, device)
        sel = sel.assign(hook=hook, layer=layer, bits=bits,
                         contexts=[" | ".join(ctxs[f]) for f in sel["feature"]],
                         neuronpedia=["" if neuronpedia_template is None else
                                      neuronpedia_template.format(layer=layer, feature=f)
                                      for f in sel["feature"]])
        picked.append(sel)
        summary.append({"hook": hook, "layer": layer, "bits": bits, "n_alive": len(t),
                        "median_corr": float(t["corr"].median()) if len(t) else float("nan"),
                        **{f"rho_{c}": _spearman(t, c) for c, _ in PROPERTIES}})

    cols = ["hook", "layer", "bits", "kind", "rank", "feature", "corr", "freq_ref", "freq_q",
            "mean_active_ref", "dec_norm", "contexts", "neuronpedia"]
    df = pd.concat(picked)[cols].reset_index(drop=True)
    df.to_csv(os.path.join(out_dir, "fragile_features.csv"), index=False)
    summ = pd.DataFrame(summary)
    summ.to_csv(os.path.join(out_dir, "fragility_summary.csv"), index=False)
    _plot_scatter(tables, summ, bits, os.path.join(out_dir, "fragility_scatter.png"))
    return df


def _plot_scatter(tables: dict[str, pd.DataFrame], summ: pd.DataFrame, bits: int, path: str):
    fig, axes = plt.subplots(len(tables), len(PROPERTIES), squeeze=False,
                             figsize=(4 * len(PROPERTIES), 3.2 * len(tables)))
    for row, (hook, t) in zip(axes, tables.items()):
        s = summ.set_index("hook").loc[hook]
        for ax, (col, label) in zip(row, PROPERTIES):
            ax.scatter(t[col], t["corr"], s=4, alpha=0.25)
            ax.set_title(f"{hook} ({bits}-bit): rho={s[f'rho_{col}']:.2f}", fontsize=9)
            ax.set_xlabel(label)
            ax.set_ylabel("feature corr")
            ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
