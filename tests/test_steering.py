import math

import pandas as pd
import torch

from quant_lens import quantize_model
from quant_lens.experiment import SimpleSAE
from quant_lens.steering import plot_steering, run_steering
from test_quant_lens import tiny_model

HOOKS = ["blocks.0.hook_resid_pre", "blocks.1.hook_resid_pre"]
quiet = lambda *_: None


def setup():
    m = tiny_model()
    saes = {h: SimpleSAE(32, 64, seed=i) for i, h in enumerate(HOOKS)}
    tokens = torch.randint(0, 100, (6, 12), generator=torch.Generator().manual_seed(0))
    return m, saes, tokens


def test_identity_quantization_transfers_perfectly():
    m, saes, tokens = setup()
    rows = run_steering(m, quantize_model(m, 16), saes, tokens, n_features=3, alphas=(0.5, 2.0),
                        min_count=1, log=quiet)
    assert len(rows) == 2 * 3 * 2
    for r in rows:
        assert r["delta_norm_ref"] > 0, "steering must change the logits"
        assert abs(r["cos_mean"] - 1) < 1e-5
        assert abs(r["kl_steered"]) < 1e-6 and abs(r["kl_clean"]) < 1e-6
        assert abs(r["effect_ratio"] - 1) < 1e-5


def test_low_bits_finite_and_drifts():
    m, saes, tokens = setup()
    rows = run_steering(m, quantize_model(m, 3), saes, tokens, n_features=3, min_count=1, log=quiet)
    for r in rows:
        assert all(math.isfinite(r[k]) for k in r if k != "hook")
        assert r["kl_clean"] > 0 and r["delta_norm_ref"] > 0


def test_feature_sampling_deterministic_and_live():
    m, saes, tokens = setup()
    q = quantize_model(m, 16)
    run = lambda **kw: run_steering(m, q, saes, tokens[:2], min_count=12, log=quiet, **kw)
    feats = lambda rows, h: [r["feature"] for r in rows if r["hook"] == h]
    a, b = run(n_features=4, seed=1), run(n_features=4, seed=1)
    assert [r["feature"] for r in a] == [r["feature"] for r in b]
    # asking for more than exist returns exactly the live set
    everything = run(n_features=10_000)
    _, cache = m.run_with_cache(tokens[:2], names_filter=HOOKS)
    for h in HOOKS:
        counts = (saes[h].encode(cache[h][:, 1:].reshape(-1, 32)) > 0).sum(0)
        live = torch.nonzero(counts >= 12).flatten().tolist()
        assert 4 < len(live) < 64
        assert feats(everything, h) == live
        assert len(feats(a, h)) == 4 and set(feats(a, h)) <= set(live)


def test_plot_steering_writes_file(tmp_path):
    m, saes, tokens = setup()
    rows = []
    for bits in (16, 3):
        for r in run_steering(m, quantize_model(m, bits), saes, tokens[:2], n_features=2,
                              min_count=1, log=quiet):
            rows.append({**r, "bits": bits, "scheme": "rtn"})
    path = tmp_path / "steering.png"
    plot_steering(pd.DataFrame(rows), str(path))
    assert path.stat().st_size > 0
