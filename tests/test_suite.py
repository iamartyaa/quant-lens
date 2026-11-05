"""End-to-end CLI path: sweep + every analysis + report, offline on a tiny model."""

import os

import pandas as pd
import pytest
import torch

from quant_lens.experiment import SimpleSAE, build_parser, run_suite
from quant_lens.report import build_report

from test_quant_lens import tiny_model

HOOKS = ["blocks.0.hook_resid_pre", "blocks.1.hook_resid_pre"]


def _inputs():
    m = tiny_model()
    saes = {h: SimpleSAE(32, 64, seed=i) for i, h in enumerate(HOOKS)}
    toks = torch.randint(0, 100, (12, 16), generator=torch.Generator().manual_seed(0))
    return m, saes, toks[:8], toks[8:]


@pytest.mark.parametrize("scheme", ["rtn", "awq_lite", "w8a8"])
def test_sweep_each_scheme(tmp_path, scheme):
    m, saes, toks, calib = _inputs()
    args = build_parser().parse_args(["--scheme", scheme, "--bits", "8", "3", "--out", str(tmp_path)])
    df = run_suite(m, saes, toks, calib, args, {h: i for i, h in enumerate(HOOKS)}, log=lambda *_: None)
    assert set(df["scheme"]) == {scheme} and len(df) == 4
    assert os.path.exists(tmp_path / "summary.png")


def test_all_analyses_and_report(tmp_path):
    m, saes, toks, calib = _inputs()
    args = build_parser().parse_args([
        "--bits", "8", "3", "--out", str(tmp_path), "--ablate-layers", "--fragility",
        "--fragility-n", "5", "--steering", "--steer-features", "3", "--steer-seqs", "4",
        "--batch-size", "4"])
    run_suite(m, saes, toks, calib, args, {h: i for i, h in enumerate(HOOKS)},
              "https://www.neuronpedia.org/gpt2-small/{layer}-res-jb/{feature}", log=lambda *_: None)
    for f in ["summary.csv", "config.json", "ablation.csv", "ablation_table.csv",
              "ablation_heatmap.png", "fragile_features.csv", "fragility_summary.csv",
              "fragility_scatter.png", "steering.csv", "steering.png"]:
        assert os.path.exists(tmp_path / f), f
    abl = pd.read_csv(tmp_path / "ablation_table.csv", index_col=0)
    assert abl.shape == (m.cfg.n_layers, len(HOOKS))
    assert set(pd.read_csv(tmp_path / "steering.csv")["bits"]) == {8, 3}
    report = open(build_report(str(tmp_path))).read()
    for section in ["Summary", "ablation", "ragility", "teering"]:
        assert section in report, section
