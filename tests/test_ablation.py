import os

import torch
from transformer_lens import HookedTransformer, HookedTransformerConfig

from quant_lens.ablation import ablation_table, run_ablation, write_ablation
from quant_lens.experiment import SimpleSAE

HOOKS = ["blocks.0.hook_resid_pre", "blocks.1.hook_resid_pre", "blocks.2.hook_resid_pre",
         "blocks.2.hook_resid_post"]


def ablation_df():
    torch.manual_seed(0)
    cfg = HookedTransformerConfig(n_layers=3, d_model=32, n_ctx=16, d_head=8, n_heads=4,
                                  d_mlp=64, d_vocab=100, act_fn="gelu", seed=0)
    m = HookedTransformer(cfg).eval()
    saes = {h: SimpleSAE(32, 128, seed=i) for i, h in enumerate(HOOKS)}
    tokens = torch.randint(0, 100, (8, 16), generator=torch.Generator().manual_seed(0))
    return run_ablation(m, saes, tokens, 3, batch_size=4, min_count=1, log=lambda *_: None)


def test_ablation_locates_drift(tmp_path):
    df = ablation_df()
    for metric in ("median_feature_corr", "jaccard"):
        t = ablation_table(df, metric)
        assert t.shape == (3, len(HOOKS))
        assert list(t.columns) == HOOKS and list(t.index) == [0, 1, 2]
        for b in range(3):
            # blocks.L.hook_resid_pre is upstream of block b iff L <= b
            for L in range(b + 1):
                assert abs(t.loc[b, f"blocks.{L}.hook_resid_pre"] - 1) < 1e-9, (metric, b, L)
            nxt = f"blocks.{b + 1}.hook_resid_pre" if b < 2 else "blocks.2.hook_resid_post"
            assert t.loc[b, nxt] < 1 - 1e-6, (metric, b)

    write_ablation(df, str(tmp_path / "out"))
    for f in ("ablation.csv", "ablation_table.csv", "ablation_heatmap.png"):
        assert os.path.getsize(tmp_path / "out" / f) > 0
