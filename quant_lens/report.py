"""Build `report.md` from the CSVs, config and figures in a results directory.

Every section is optional: it is written only when its input files exist.
The report holds numbers read from the files plus fixed metric definitions.

    python -m quant_lens.report results/
"""

from __future__ import annotations

import argparse
import json
import os

import pandas as pd

SUMMARY_COLS = ["hook", "bits", "scheme", "group_size", "median_feature_corr", "jaccard",
                "topk_overlap", "lost_frac", "new_frac", "fve_ref", "fve_q", "logit_kl", "top1_agree"]
FRAGILE_COLS = ["hook", "kind", "rank", "feature", "corr", "freq_ref", "mean_active_ref",
                "neuronpedia", "contexts"]
STEER_METRICS = ["cos_mean", "kl_steered", "kl_clean", "effect_ratio"]


def _cell(v) -> str:
    if isinstance(v, float):
        return "" if pd.isna(v) else f"{v:.4g}"
    return str(v).replace("\n", " ").replace("|", "\\|")


def md_table(df: pd.DataFrame) -> str:
    """GitHub markdown table; floats to 4 significant digits, `|` and newlines escaped."""
    lines = ["| " + " | ".join(_cell(c) for c in df.columns) + " |",
             "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |"
              for row in df.itertuples(index=False, name=None)]
    return "\n".join(lines)


def _link(url) -> str:
    return f"[link]({url})" if isinstance(url, str) and url else ""


def _truncate(s, n: int = 80) -> str:
    s = "" if pd.isna(s) else " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def build_report(results_dir: str) -> str:
    """Write `<results_dir>/report.md` and return its path."""
    path = lambda name: os.path.join(results_dir, name)
    has = lambda name: os.path.exists(path(name))
    fig = lambda name, alt: [f"![{alt}]({name})", ""] if has(name) else []
    out = ["# quant-lens report", ""]

    if has("config.json"):
        with open(path("config.json")) as f:
            cfg = json.load(f)
        kv = pd.DataFrame({"key": list(cfg), "value": [json.dumps(v) if isinstance(v, (list, dict)) else v
                                                       for v in cfg.values()]})
        out += ["## Run config", "", md_table(kv), ""]

    if has("summary.csv"):
        df = pd.read_csv(path("summary.csv"))
        out += ["## Summary", "",
                "One row per (hook, bit width). `median_feature_corr`: median over live features of the "
                "Pearson correlation of activations, reference vs quantized. `jaccard`: per-token overlap "
                "of active feature sets. `topk_overlap`: per-token overlap of the 10 strongest features. "
                "`lost_frac` / `new_frac`: features that stop firing / only fire after quantization. "
                "`fve_ref` / `fve_q`: residual-stream variance explained by the SAE. `logit_kl`, "
                "`top1_agree`: output drift on the same tokens.", "",
                md_table(df[[c for c in SUMMARY_COLS if c in df.columns]]), ""]
        out += fig("summary.png", "summary")

    if has("ablation_table.csv"):
        df = pd.read_csv(path("ablation_table.csv"), index_col=0)
        df.index.name = df.index.name or "quantized_block"
        out += ["## Layer ablation", "",
                "Each row quantizes one transformer block and keeps the rest in full precision; columns "
                "are the hooks where features are compared, cells one metric (`median_feature_corr` by "
                "default). Long-format data is in `ablation.csv`.", "",
                md_table(df.reset_index()), ""]
        out += fig("ablation_heatmap.png", "ablation heatmap")

    if has("fragility_summary.csv") or has("fragile_features.csv"):
        out += ["## Feature fragility", "",
                "`corr`: Pearson correlation of one feature's activations, reference vs quantized. "
                "`freq_ref`: fraction of tokens it fires on; `mean_active_ref`: mean activation where it "
                "fires. `rho_*`: Spearman rank correlation of `corr` with that property over alive "
                "features (positive: larger values are more robust). Fragile features have the lowest "
                "`corr`, robust the highest; `rank` 0 is the most extreme.", ""]
        if has("fragility_summary.csv"):
            out += [md_table(pd.read_csv(path("fragility_summary.csv"))), ""]
        out += fig("fragility_scatter.png", "fragility scatter")
        if has("fragile_features.csv"):
            ff = pd.read_csv(path("fragile_features.csv"))
            if "neuronpedia" in ff:
                ff["neuronpedia"] = ff["neuronpedia"].map(_link)
            if "contexts" in ff:
                ff["contexts"] = ff["contexts"].map(_truncate)
            ff = ff[[c for c in FRAGILE_COLS if c in ff.columns]]
            for (hook, kind), g in ff.groupby(["hook", "kind"], sort=False):
                g = g.sort_values("rank").head(10) if "rank" in g else g.head(10)
                out += [f"### {hook}: {kind}", "", md_table(g), ""]

    if has("steering.csv"):
        df = pd.read_csv(path("steering.csv"))
        keys = [c for c in ("hook", "bits", "scheme", "alpha") if c in df.columns]
        med = df.groupby(keys, as_index=False)[[c for c in STEER_METRICS if c in df.columns]].median()
        out += ["## Steering transfer", "",
                "Medians over steered features per (hook, bits, alpha); alpha is in units of the mean "
                "residual norm. `cos_mean`: cosine between the log-prob change steering causes in the "
                "reference and in the quantized model. `kl_steered` / `kl_clean`: KL(reference || "
                "quantized) with and without steering. `effect_ratio`: norm of the quantized log-prob "
                "change over the reference one.", "",
                md_table(med), ""]
        out += fig("steering.png", "steering")

    report = path("report.md")
    with open(report, "w") as f:
        f.write("\n".join(out))
    return report


def main() -> None:
    """CLI: python -m quant_lens.report [results_dir]."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results_dir", nargs="?", default="results")
    print(f"wrote {build_report(p.parse_args().results_dir)}")


if __name__ == "__main__":
    main()
