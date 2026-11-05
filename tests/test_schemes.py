import pytest
import torch

import quant_lens.quantize as qz
from quant_lens.quantize import (INPUT_HOOKS, SCHEMES, awq_lite_weights, fake_quantize,
                                 fake_quantize_act, quantize_model)
from test_quant_lens import tiny_model


def tokens(n=8, seed=0):
    return torch.randint(0, 100, (n, 32), generator=torch.Generator().manual_seed(seed))


def logit_mse(m, q, toks):
    return (m(toks) - q(toks)).pow(2).mean().item()


def build(m, scheme, bits, **kw):
    act_bits = bits if scheme == "w8a8" else 8
    return quantize_model(m, bits, scheme=scheme, calib_tokens=tokens(), act_bits=act_bits, **kw)


def hooked(model):
    return {name for name, hp in model.hook_dict.items() if hp.fwd_hooks}


@pytest.mark.parametrize("scheme", SCHEMES)
def test_16bit_is_identity(scheme):
    m = tiny_model()
    q = build(m, scheme, 16)
    assert torch.equal(m(tokens()), q(tokens()))
    assert not hooked(q)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_error_shrinks_with_more_bits(scheme):
    m = tiny_model()
    errs = [logit_mse(m, build(m, scheme, b), tokens(seed=1)) for b in (3, 4, 6, 8)]
    assert all(a > b for a, b in zip(errs, errs[1:])), errs


def toy_layer(seed):
    """Input with a few large-magnitude channels, as in LLM activations."""
    g = torch.Generator().manual_seed(seed)
    x = torch.randn(512, 64, generator=g)
    x[:, :4] *= 20
    return x


def test_awq_lite_beats_rtn_on_salient_channels():
    w = torch.randn(64, 32, generator=torch.Generator().manual_seed(0))
    x = toy_layer(1)
    (wq,), alpha = awq_lite_weights([w], x, 3)
    assert alpha > 0
    for xe in (x, toy_layer(2)):
        rtn = (xe @ (w - fake_quantize(w, 3))).pow(2).mean()
        awq = (xe @ (w - wq)).pow(2).mean()
        assert awq < 0.8 * rtn, (awq, rtn)


def test_awq_lite_shared_group_and_head_shapes():
    g = torch.Generator().manual_seed(0)
    x = toy_layer(1)
    ws = [torch.randn(4, 64, 8, generator=g) for _ in range(3)]  # Q, K, V
    qs, _ = awq_lite_weights(ws, x, 4)
    assert [q.shape for q in qs] == [w.shape for w in ws]
    z = torch.randn(256, 4, 8, generator=g)  # W_O: input channel = (head, d_head)
    (wo,), _ = awq_lite_weights([torch.randn(4, 8, 64, generator=g)], z, 4)
    assert wo.shape == (4, 8, 64)


def test_fake_quantize_act_is_per_token():
    x = torch.randn(2, 5, 4, 8)
    x[0, 0] *= 1000  # an outlier token must not affect the others
    q = fake_quantize_act(x, 8)
    assert torch.allclose(q[1:], fake_quantize_act(x[1:], 8))
    assert torch.equal(fake_quantize_act(x, 16), x)
    assert (q - x).abs().max() <= x.abs().amax((2, 3), keepdim=True).max() / 127


def test_w8a8_hooks_persist():
    m = tiny_model()
    q = quantize_model(m, 8, scheme="w8a8", act_bits=4)
    toks = tokens()
    assert not hooked(m)
    assert hooked(q) == {f"blocks.{i}.{h}" for i in range(2) for h in set(INPUT_HOOKS.values())}
    plain = q(toks)
    assert not torch.equal(plain, quantize_model(m, 8)(toks)), "act quantization must matter"
    logits, _ = q.run_with_cache(toks)
    assert torch.equal(logits, plain)
    assert torch.equal(q.run_with_hooks(toks, fwd_hooks=[]), plain)
    assert hooked(q) and torch.equal(q(toks), plain)


@pytest.mark.parametrize("scheme", ["awq_lite", "awq_seq", "w8a8"])
def test_blocks_restricts_quantization(scheme):
    m = tiny_model()
    q = build(m, scheme, 4, blocks=[1])
    ref, qp = dict(m.named_parameters()), dict(q.named_parameters())
    for name in ("attn.W_Q", "attn.W_O", "mlp.W_in", "mlp.W_out"):
        assert torch.equal(qp[f"blocks.0.{name}"], ref[f"blocks.0.{name}"])
        assert not torch.equal(qp[f"blocks.1.{name}"], ref[f"blocks.1.{name}"])
    assert all(h.startswith("blocks.1.") for h in hooked(q))
    assert bool(hooked(q)) == (scheme == "w8a8")


def test_bad_arguments_raise():
    m = tiny_model()
    with pytest.raises(ValueError):
        quantize_model(m, 4, scheme="gptq")
    for scheme in ("awq_lite", "awq_seq"):
        with pytest.raises(ValueError):
            quantize_model(m, 4, scheme=scheme)


def test_w_o_scales_span_heads():
    # W_O is one [n_heads * d_head, d_model] layer: a single scale per output
    # column across heads, and groups may be larger than d_head
    m = tiny_model()
    w = quantize_model(m, 4).blocks[0].attn.W_O.detach()
    for j in range(w.shape[-1]):
        assert w[..., j].unique().numel() <= 15
    g = quantize_model(m, 4, group_size=16).blocks[0].attn.W_O
    assert g.shape == m.blocks[0].attn.W_O.shape


def test_awq_seq_differs_from_awq_lite_after_block_0():
    m = tiny_model()
    lite, seq = dict(build(m, "awq_lite", 3).named_parameters()), dict(build(m, "awq_seq", 3).named_parameters())
    for name in ("attn.W_Q", "attn.W_O", "mlp.W_in", "mlp.W_out"):
        assert torch.equal(lite[f"blocks.0.{name}"], seq[f"blocks.0.{name}"])
    assert any(not torch.equal(lite[f"blocks.1.{n}"], seq[f"blocks.1.{n}"])
               for n in ("attn.W_Q", "attn.W_O", "mlp.W_in", "mlp.W_out"))


def test_awq_seq_calibrates_on_quantized_prefix(monkeypatch):
    m = tiny_model()
    calls = []
    real = qz._calib_inputs

    def spy(model, toks, hooks, batch_size):
        xs = real(model, toks, hooks, batch_size)
        calls.append((model, hooks, xs))
        return xs
    monkeypatch.setattr(qz, "_calib_inputs", spy)
    q = build(m, "awq_seq", 3)
    assert [{h.split(".")[1] for h in hooks} for _, hooks, _ in calls] == [{"0"}, {"1"}]
    assert all(model is q for model, _, _ in calls)
    for model, hooks, xs in calls:
        fp = real(m, tokens(), hooks, 8)
        same = [torch.equal(xs[h], fp[h]) for h in hooks]
        assert all(same) if hooks[0].startswith("blocks.0.") else not any(same)
