"""Plot how feature agreement degrades with fewer bits, one line per layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib

if TYPE_CHECKING:
    import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PANELS = [
    ("median_feature_corr", "Median feature correlation"),
    ("jaccard", "Active-set Jaccard per token"),
    ("lost_frac", "Features lost (fraction)"),
    ("fve_q", "SAE variance explained (quantized)"),
]


def plot_summary(df: "pd.DataFrame", path: str) -> None:
    """One panel per metric in PANELS, one line per hook, bits on a reversed x axis."""
    fig, axes = plt.subplots(1, len(PANELS), figsize=(4 * len(PANELS), 3.4))
    hooks = list(dict.fromkeys(df["hook"]))  # run order, i.e. by layer
    colors = plt.cm.viridis([i / max(len(hooks) - 1, 1) for i in range(len(hooks))])
    for ax, (col, title) in zip(axes, PANELS):
        for hook, color in zip(hooks, colors):
            g = df[df["hook"] == hook].sort_values("bits", ascending=False)
            ax.plot(g["bits"], g[col], marker="o", color=color,
                    label=hook.replace(".hook_resid_pre", ""))
        if col == "fve_q" and df[col].min() < -1:
            ax.set_yscale("symlog", linthresh=1)  # very negative FVE would flatten the rest
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("weight bits")
        ax.set_xticks(sorted(df["bits"].unique()))
        ax.invert_xaxis()
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
