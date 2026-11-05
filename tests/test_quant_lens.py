import torch
from transformer_lens import HookedTransformer, HookedTransformerConfig

from quant_lens import FeatureComparison, fake_quantize, logit_stats, quantize_model, weight_error
from quant_lens.experiment import SimpleSAE, run_experiment


def tiny_model(seed=0):
    torch.manual_seed(seed)
    cfg = HookedTransformerConfig(n_layers=2, d_model=32, n_ctx=32, d_head=8, n_heads=4,
                                  d_mlp=64, d_vocab=100, act_fn="gelu", seed=seed)
    return HookedTransformer(cfg).eval()


# ---------- quantization ----------

def test_16bit_is_identity():
    w = torch.randn(4, 32, 8)
    assert torch.equal(fake_quantize(w, 16), w)


def test_values_lie_on_grid():
    w = torch.randn(64, 16)
    q = fake_quantize(w, 4)
    # each output column has at most 2*7+1 = 15 distinct levels
    for j in range(w.shape[1]):
        assert q[:, j].unique().numel() <= 15


def test_error_shrinks_with_more_bits():
    w = torch.randn(128, 64)
    errs = [(fake_quantize(w, b) - w).norm() / w.norm() for b in (3, 4, 6, 8)]
    assert all(a > b for a, b in zip(errs, errs[1:]))
    assert errs[-1] < 0.01


def test_group_wise_is_finer_than_per_channel():
    w = torch.randn(128, 64)
    per_ch = (fake_quantize(w, 4) - w).norm()
    grouped = (fake_quantize(w, 4, group_size=32) - w).norm()
    assert grouped < per_ch
    assert fake_quantize(w, 4, group_size=32).shape == w.shape


def test_zero_weights_survive():
    w = torch.zeros(8, 4)
    assert torch.equal(fake_quantize(w, 4), w)


def test_quantize_model_copies_and_targets():
    m = tiny_model()
    before = m.blocks[0].attn.W_Q.detach().clone()
    q = quantize_model(m, 4)
    assert torch.equal(m.blocks[0].attn.W_Q, before), "original model must not change"
    assert not torch.equal(q.blocks[0].attn.W_Q, before)
    assert torch.equal(q.embed.W_E, m.embed.W_E), "embeddings stay full precision"
    assert torch.equal(q.blocks[0].attn.b_Q, m.blocks[0].attn.b_Q), "biases stay full precision"
    assert weight_error(m, quantize_model(m, 16)) == 0.0
    assert weight_error(m, q) > weight_error(m, quantize_model(m, 8))


# ---------- metrics ----------

def sparse_acts(n=200, d=50, seed=0):
    g = torch.Generator().manual_seed(seed)
    return torch.relu(torch.randn(n, d, generator=g) - 1.0)


def test_identical_acts_give_perfect_scores():
    a = sparse_acts()
    c = FeatureComparison(a.shape[1], topk=5, min_count=1)
    c.update(a, a.clone(), a, a, a, a)
    r = c.result()
    assert abs(r.median_feature_corr - 1) < 1e-6
    assert abs(r.jaccard - 1) < 1e-9
    assert abs(r.topk_overlap - 1) < 1e-9
    assert r.lost_frac == 0 and r.new_frac == 0
    assert abs(r.fve_ref - 1) < 1e-9 and abs(r.fve_q - 1) < 1e-9


def test_all_features_lost():
    a = sparse_acts()
    c = FeatureComparison(a.shape[1], min_count=1)
    c.update(a, torch.zeros_like(a))
    assert c.result().lost_frac == 1.0


def test_streaming_matches_single_batch():
    a = sparse_acts(seed=1)
    b = torch.relu(a + 0.3 * torch.randn_like(a))
    x = torch.randn(a.shape[0], 16)
    rx = x + 0.2 * torch.randn_like(x)
    one = FeatureComparison(a.shape[1], topk=5, min_count=1)
    one.update(a, b, x, rx, x, rx)
    many = FeatureComparison(a.shape[1], topk=5, min_count=1)
    for i in range(0, a.shape[0], 37):
        s = slice(i, i + 37)
        many.update(a[s], b[s], x[s], rx[s], x[s], rx[s])
    r1, r2 = one.result().as_dict(), many.result().as_dict()
    for k in r1:
        assert abs(r1[k] - r2[k]) < 1e-6, k


def test_logit_stats_identical():
    x = torch.randn(3, 5, 20)
    kl, agree, n = logit_stats(x, x.clone())
    assert abs(kl) < 1e-9 and agree == n == 15


# ---------- end to end ----------

def test_end_to_end_tiny_model():
    m = tiny_model()
    hooks = ["blocks.0.hook_resid_pre", "blocks.1.hook_resid_pre"]
    saes = {h: SimpleSAE(32, 128, seed=i) for i, h in enumerate(hooks)}
    tokens = torch.randint(0, 100, (16, 32), generator=torch.Generator().manual_seed(0))
    rows = run_experiment(m, saes, tokens, [16, 8, 3], batch_size=4, min_count=1, log=lambda *_: None)
    by = {(r["hook"], r["bits"]): r for r in rows}
    assert len(rows) == 6
    # layer 0's residual stream is just the embedding, which is never quantized
    assert abs(by[(hooks[0], 3)]["jaccard"] - 1) < 1e-9
    # 16-bit is the identity, so layer 1 matches exactly
    r16 = by[(hooks[1], 16)]
    assert abs(r16["jaccard"] - 1) < 1e-9 and abs(r16["logit_kl"]) < 1e-9
    # aggressive quantization must disturb layer 1 features and logits
    r3 = by[(hooks[1], 3)]
    assert r3["jaccard"] < 1 and r3["logit_kl"] > 0
    assert by[(hooks[1], 8)]["jaccard"] >= r3["jaccard"]
