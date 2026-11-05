"""Layer ablation: where does feature drift start?

Quantize ONE transformer block at a time, keep every other block in full
precision, and measure feature drift at every SAE hook. A hook upstream of the
quantized block sees exactly the reference residual stream, so drift there is
zero by construction; the first hook after the block shows how much that block
alone disturbs the features, and later hooks show whether the error grows or
heals as it propagates.

The result is a [quantized block x measured hook] table of one metric
(default `median_feature_corr`), written as CSV and a heatmap.
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import torch  # noqa: E402

from .experiment import run_experiment  # noqa: E402


def run_ablation(model, saes: dict[str, object], tokens: torch.Tensor, bits: int,
                 blocks: list[int] | None = None, *, group_size: int | None = None,
                 scheme: str = "rtn", calib_tokens: torch.Tensor | None = None, act_bits: int = 8,
                 batch_size: int = 8, topk: int = 10, min_count: int = 5, device: str = "cpu",
                 log=print) -> pd.DataFrame:
    """Quantize each block in `blocks` alone at `bits` and measure every SAE hook.

    Args:
        saes: mapping from hook name to SAE, as for `run_experiment`.
        blocks: blocks to ablate one at a time (None = all of them).
    Returns the `run_experiment` rows plus a "quantized_block" column.
    """
    if blocks is None:
        blocks = list(range(model.cfg.n_layers))
    rows = []
    for b in blocks:
        log(f"[ablation] quantizing block {b} only at {bits} bits")
        # ponytail: the reference forward is recomputed for every block (n_blocks x the
        # reference cost); cache reference SAE acts once if Gemma-scale ablation is slow.
        for row in run_experiment(model, saes, tokens, [bits], group_size, batch_size, topk,
                                  min_count, device, log=log, scheme=scheme,
                                  calib_tokens=calib_tokens, act_bits=act_bits, blocks=[b]):
            rows.append({**row, "quantized_block": b})
    return pd.DataFrame(rows)


def ablation_table(df: pd.DataFrame, metric: str = "median_feature_corr") -> pd.DataFrame:
    """Pivot to index quantized_block x columns hook (in first-appearance order)."""
    table = df.pivot(index="quantized_block", columns="hook", values=metric)
    return table[list(df["hook"].unique())]


def _short_labels(hooks: list[str]) -> list[str]:
    """"blocks.6.hook_resid_pre" -> "L6"; keep the hook name if hooks differ in it."""
    names = [h.split(".", 2) for h in hooks]
    same_suffix = len({n[-1] for n in names}) == 1
    out = []
    for h, n in zip(hooks, names):
        if n[0] != "blocks" or len(n) < 3:
            out.append(h)
        else:
            out.append(f"L{n[1]}" if same_suffix else f"L{n[1]} {n[2].removeprefix('hook_')}")
    return out


def plot_ablation_heatmap(table: pd.DataFrame, path: str, metric: str = "median_feature_corr"):
    """Heatmap of `table`: rows quantized block, columns measured hook."""
    n_rows, n_cols = table.shape
    fig, ax = plt.subplots(figsize=(1.2 * n_cols + 2.5, 0.6 * n_rows + 1.8))
    im = ax.imshow(table.values, cmap="viridis", aspect="auto")
    ax.set_xticks(range(n_cols), _short_labels(list(table.columns)), rotation=45, ha="right")
    ax.set_yticks(range(n_rows), [str(b) for b in table.index])
    ax.set_xlabel("measured hook")
    ax.set_ylabel("quantized block")
    lo, hi = im.get_clim()
    for i in range(n_rows):
        for j in range(n_cols):
            v = table.values[i, j]
            # dark text on the bright end of viridis, light text elsewhere
            color = "black" if hi > lo and (v - lo) / (hi - lo) > 0.6 else "white"
            ax.text(j, i, f"{v:.3f}", ha="center", va="center", fontsize=8, color=color)
    fig.colorbar(im, ax=ax, label=metric)
    ax.set_title(f"{metric} when one block is quantized", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_ablation(df: pd.DataFrame, out_dir: str, metric: str = "median_feature_corr") -> None:
    """Write ablation.csv (long), ablation_table.csv and ablation_heatmap.png to `out_dir`."""
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "ablation.csv"), index=False)
    table = ablation_table(df, metric)
    table.to_csv(os.path.join(out_dir, "ablation_table.csv"))
    plot_ablation_heatmap(table, os.path.join(out_dir, "ablation_heatmap.png"), metric)
