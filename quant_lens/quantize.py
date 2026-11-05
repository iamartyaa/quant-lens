"""Weight quantization for TransformerLens models.

We use simulated ("fake") quantization: each weight is rounded to a b-bit
integer grid and immediately dequantized back to float. The forward pass then
runs in float, but every weight value is one the quantized model could hold.
This isolates the effect of the *weight rounding* itself, without mixing in
differences between low-bit kernels.

Scheme: symmetric round-to-nearest (RTN). The scale is computed per output
channel, or per group of `group_size` input rows within each output channel.
Biases, embeddings, unembedding and LayerNorm weights stay in full precision,
which matches common practice for weight-only quantization.
"""

from __future__ import annotations

import copy

import torch

# TransformerLens stores weights so that the layer computes x @ W.
# The input dimension is always axis -2, so per-output-channel scaling means
# taking the max over axis -2 (for every head / output column separately).
# `_W_K` / `_W_V` are the grouped-query attention names (e.g. Gemma 2) and
# `W_gate` is the gated-MLP input projection.
DEFAULT_TARGETS = ("W_Q", "W_K", "W_V", "_W_K", "_W_V", "W_O", "W_in", "W_gate", "W_out")
SCHEMES = ("rtn",)


def block_of(name: str) -> int | None:
    """Block index of a parameter name like `blocks.3.attn.W_Q`, else None."""
    parts = name.split(".")
    return int(parts[1]) if parts[0] == "blocks" else None


def fake_quantize(w: torch.Tensor, bits: int, group_size: int | None = None) -> torch.Tensor:
    """Symmetric round-to-nearest quantize-dequantize of `w`.

    Args:
        w: weight tensor whose input dimension is axis -2.
        bits: bit width, e.g. 8 or 4. Values >= 16 return `w` unchanged.
        group_size: if given, scales are computed per group of this many input
            rows (like GPTQ / AWQ group-wise schemes) instead of per channel.
    """
    if bits >= 16:
        return w.clone()
    if bits < 2:
        raise ValueError("bits must be >= 2")

    qmax = 2 ** (bits - 1) - 1  # e.g. 127 for int8, 7 for int4
    w_f = w.float()

    if group_size is None:
        amax = w_f.abs().amax(dim=-2, keepdim=True)
        scale = (amax / qmax).clamp(min=1e-12)
        q = torch.clamp(torch.round(w_f / scale), -qmax, qmax)
        return (q * scale).to(w.dtype)

    d_in = w_f.shape[-2]
    if d_in % group_size != 0:
        raise ValueError(f"input dim {d_in} not divisible by group_size {group_size}")
    lead = w_f.shape[:-2]
    d_out = w_f.shape[-1]
    g = w_f.reshape(*lead, d_in // group_size, group_size, d_out)
    amax = g.abs().amax(dim=-2, keepdim=True)
    scale = (amax / qmax).clamp(min=1e-12)
    q = torch.clamp(torch.round(g / scale), -qmax, qmax)
    return (q * scale).reshape(w_f.shape).to(w.dtype)


@torch.no_grad()
def quantize_model(model, bits: int, group_size: int | None = None,
                   targets: tuple[str, ...] = DEFAULT_TARGETS, *, scheme: str = "rtn",
                   calib_tokens: torch.Tensor | None = None, act_bits: int = 8,
                   blocks: list[int] | None = None, batch_size: int = 8):
    """Return a deep copy of a TransformerLens model with quantized weights.

    The original model is left untouched, so the same reference model can be
    compared against several bit widths.

    Args:
        scheme: one of `SCHEMES`.
        calib_tokens: [n, ctx] calibration tokens, needed by activation-aware schemes.
        act_bits: activation bit width for schemes that quantize activations.
        blocks: only quantize these transformer blocks (None = all).
    """
    if scheme not in SCHEMES:
        raise ValueError(f"unknown scheme {scheme!r}, expected one of {SCHEMES}")
    qmodel = copy.deepcopy(model)
    for name, param in qmodel.named_parameters():
        if name.split(".")[-1] in targets and (blocks is None or block_of(name) in blocks):
            param.copy_(fake_quantize(param.data, bits, group_size))
    return qmodel


@torch.no_grad()
def weight_error(model, qmodel, targets: tuple[str, ...] = DEFAULT_TARGETS) -> float:
    """Relative Frobenius error ||W - Wq|| / ||W||, pooled over target weights."""
    num, den = 0.0, 0.0
    q_params = dict(qmodel.named_parameters())
    for name, p in model.named_parameters():
        if name.split(".")[-1] in targets:
            num += (p.float() - q_params[name].float()).pow(2).sum().item()
            den += p.float().pow(2).sum().item()
    return (num / den) ** 0.5 if den > 0 else 0.0
