import json

import pandas as pd

from quant_lens.report import build_report, md_table


def write_all(d):
    (d / "config.json").write_text(json.dumps({"model": "gpt2", "layers": [2, 6], "bits": [8, 4]}))
    pd.DataFrame({"hook": ["blocks.6.hook_resid_pre"] * 2, "bits": [8, 4], "scheme": "rtn",
                  "group_size": 0, "median_feature_corr": [0.987654321, 0.5], "jaccard": [0.9, 0.4],
                  "weight_rel_err": [0.01, 0.1]}).to_csv(d / "summary.csv", index=False)
    (d / "summary.png").write_bytes(b"")
    pd.DataFrame({"blocks.2.hook_resid_pre": [1.0, 1.0], "blocks.6.hook_resid_pre": [0.95, 0.97]},
                 index=pd.Index([0, 1], name="quantized_block")).to_csv(d / "ablation_table.csv")
    pd.DataFrame({"hook": ["h"], "n_alive": [100], "median_corr": [0.8],
                  "rho_log10_freq_ref": [0.31]}).to_csv(d / "fragility_summary.csv", index=False)
    rows = [{"hook": "h", "kind": k, "rank": r, "feature": 10 * r + (k == "robust"), "corr": 0.1 * r,
             "freq_ref": 0.01, "mean_active_ref": 2.0,
             "neuronpedia": f"https://www.neuronpedia.org/gpt2-small/6-res-jb/{r}",
             "contexts": "a «b» | c " + "x" * 200}
            for k in ("fragile", "robust") for r in range(12)]
    pd.DataFrame(rows).to_csv(d / "fragile_features.csv", index=False)
    pd.DataFrame({"hook": "h", "bits": 4, "feature": [1, 2, 3], "alpha": 2.0,
                  "cos_mean": [0.1, 0.2, 0.9], "kl_steered": 0.3, "kl_clean": 0.1,
                  "effect_ratio": 1.0}).to_csv(d / "steering.csv", index=False)


def test_full_report(tmp_path):
    write_all(tmp_path)
    text = open(build_report(str(tmp_path))).read()
    for h in ("## Run config", "## Summary", "## Layer ablation", "## Feature fragility",
              "## Steering transfer", "### h: fragile", "### h: robust"):
        assert h in text, h
    assert "0.9877" in text and "weight_rel_err" not in text  # 4 sig digits, only listed columns
    assert "![summary](summary.png)" in text and "ablation_heatmap.png" not in text
    assert "| quantized_block |" in text and "0.31" in text
    assert "[link](https://www.neuronpedia.org/gpt2-small/6-res-jb/0)" in text
    assert "| 0.2 |" in text  # median cos_mean of 0.1, 0.2, 0.9
    frag = text.split("### h: fragile")[1].split("###")[0]
    # header, separator, 10 rows; every row has the same number of unescaped pipes
    lines = [ln for ln in frag.splitlines() if ln.startswith("|")]
    assert len(lines) == 12
    n_pipes = {ln.replace("\\|", "").count("|") for ln in lines}
    assert len(n_pipes) == 1
    assert "\\|" in frag and "x" * 100 not in frag


def test_missing_files_are_skipped(tmp_path):
    text = open(build_report(str(tmp_path))).read()
    assert "##" not in text
    pd.DataFrame({"hook": ["h"], "bits": [4], "jaccard": [0.5]}).to_csv(tmp_path / "summary.csv", index=False)
    text = open(build_report(str(tmp_path))).read()
    assert "## Summary" in text and "## Steering" not in text and "summary.png" not in text


def test_md_table_escapes_and_formats():
    t = md_table(pd.DataFrame({"a": ["x|y\nz"], "b": [1.23456789], "c": [float("nan")], "d": [3]}))
    assert t.splitlines()[2] == "| x\\|y z | 1.235 |  | 3 |"
