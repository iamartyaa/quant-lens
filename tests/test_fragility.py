import os

import pandas as pd
import torch

from quant_lens.experiment import SimpleSAE, run_experiment
from quant_lens.fragility import analyze_fragility, feature_table, top_contexts
from test_quant_lens import tiny_model

HOOKS = ["blocks.0.hook_resid_pre", "blocks.1.hook_resid_pre"]


def setup():
    m = tiny_model()
    saes = {h: SimpleSAE(32, 64, seed=i) for i, h in enumerate(HOOKS)}
    tokens = torch.randint(0, 100, (8, 16), generator=torch.Generator().manual_seed(0))
    return m, saes, tokens


def test_analyze_fragility(tmp_path):
    m, saes, tokens = setup()
    pf_dir = str(tmp_path / "pf")
    run_experiment(m, saes, tokens, [3], batch_size=4, min_count=1, per_feature_dir=pf_dir,
                   log=lambda *_: None)
    n = 5
    out = str(tmp_path / "out")
    df = analyze_fragility(m, saes, tokens, pf_dir, 3, out, {h: i for i, h in enumerate(HOOKS)},
                           n=n, batch_size=4, min_count=1)
    csv = pd.read_csv(os.path.join(out, "fragile_features.csv"), keep_default_na=False)
    assert len(csv) == len(df)
    for i, h in enumerate(HOOKS):
        g = csv[csv["hook"] == h]
        assert 0 < len(g) <= 2 * n
        frag, rob = g[g["kind"] == "fragile"], g[g["kind"] == "robust"]
        assert frag["corr"].max() <= rob["corr"].min()
        assert list(frag["rank"]) == list(range(len(frag)))
        assert frag["corr"].is_monotonic_increasing and rob["corr"].is_monotonic_decreasing
        for f, link in zip(g["feature"], g["neuronpedia"]):
            assert link == f"https://www.neuronpedia.org/gpt2-small/{i}-res-jb/{f}"
        assert all(c and "«" in c for c in g["contexts"])
    summ = pd.read_csv(os.path.join(out, "fragility_summary.csv"))
    assert list(summ["hook"]) == HOOKS and (summ["n_alive"] > 0).all()
    assert os.path.getsize(os.path.join(out, "fragility_scatter.png")) > 0

    # n larger than the alive count: split without overlap, no template
    df = analyze_fragility(m, saes, tokens, pf_dir, 3, out, {h: 0 for h in HOOKS}, n=10_000,
                           neuronpedia_template=None, batch_size=4, min_count=1)
    for h in HOOKS:
        g = df[df["hook"] == h]
        assert g["feature"].is_unique and len(g) == summ.set_index("hook").loc[h, "n_alive"]
    assert (df["neuronpedia"] == "").all()


def test_feature_table_filters_dead_and_nan():
    sae = SimpleSAE(4, 3)
    pf = {"freq_ref": torch.tensor([0.5, 0.01, 0.5]), "freq_q": torch.tensor([0.5, 0.0, 0.5]),
          "corr": torch.tensor([0.9, 0.5, float("nan")]),
          "mean_active_ref": torch.tensor([1.0, 2.0, 3.0]), "n_tokens": 100}
    t = feature_table(pf, sae, min_count=5)
    assert list(t["feature"]) == [0]
    assert abs(t["dec_norm"][0] - sae.W_dec[0].norm().item()) < 1e-6


def test_top_contexts_matches_brute_force():
    m, saes, tokens = setup()
    hook, sae = HOOKS[1], saes[HOOKS[1]]
    feats = [0, 7, 21]
    k, window = 3, 4
    got = top_contexts(m, sae, hook, tokens, feats, k=k, window=window, batch_size=3)
    _, cache = m.run_with_cache(tokens, names_filter=[hook])
    acts = sae.encode(cache[hook]).detach()
    acts[:, 0] = -1  # BOS excluded
    for f in feats:
        a = acts[..., f].flatten()
        top = a.topk(k).indices.tolist()
        want = []
        for n in top:
            if a[n] <= 0:
                continue
            s, p = divmod(n, tokens.shape[1])
            before = tokens[s, max(0, p - window):p].tolist()
            want.append(" ".join(map(str, before)) + f" «{tokens[s, p].item()}»")
        assert got[f] == want and want
