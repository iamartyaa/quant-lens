"""Plot how feature agreement degrades with fewer bits, one line per layer."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PANELS = [
    ("median_feature_corr", "Median feature correlation"),
    ("jaccard", "Active-set Jaccard per token"),
    ("lost_frac", "Features lost (fraction)"),
    ("fve_q", "SAE variance explained (quantized)"),
]


def plot_summary(df, path: str):
    fig, axes = plt.subplots(1, len(PANELS), figsize=(4 * len(PANELS), 3.4))
    for ax, (col, title) in zip(axes, PANELS):
        for hook, g in df.groupby("hook"):
            g = g.sort_values("bits", ascending=False)
            ax.plot(g["bits"], g[col], marker="o", label=hook.replace(".hook_resid_pre", ""))
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("weight bits")
        ax.invert_xaxis()
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
