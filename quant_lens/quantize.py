"""Weight quantization for TransformerLens models.

We use simulated ("fake") quantization: each weight is rounded to a b-bit
integer grid and immediately dequantized back to float. The forward pass then
runs in float, but every weight value is one the quantized model could hold.
This isolates the effect of the *weight rounding* itself, without mixing in
differences between low-bit kernels.

Biases, embeddings, unembedding and LayerNorm weights stay in full precision,
which matches common practice. Schemes (`SCHEMES`):

- rtn: symmetric round-to-nearest. The scale is computed per output channel,
  or per group of `group_size` input rows within each output channel.
- awq_lite: activation-aware scaling in the style of AWQ (Lin et al. 2023),
  without its clipping search. On calibration tokens we record the input of
  every quantized linear and its per-input-channel mean |x|. For a grid of
  alpha in [0, 1] the input channels are scaled by s = mean|x| ** alpha, the
  weight is quantized as RTN(W * s) / s, and the alpha with the lowest output
  MSE ||xW - xWq||^2 is kept. Dividing by s after dequantizing is exactly
  equivalent to folding 1/s into the previous op (LayerNorm, or W_V / W_in for
  W_O / W_out) as real AWQ does, so we never touch the previous op. Weights
  that read the same input (Q, K, V; W_in and W_gate) share one s and alpha.
  alpha = 0 is plain RTN, so on the calibration data awq_lite is never worse.
  Every layer is calibrated on full-precision activations.
- awq_seq: awq_lite with sequential (GPTQ-style, error-aware) calibration.
  Blocks are processed in order, and block i is calibrated on inputs from the
  copy whose earlier blocks are already quantized, captured in one pass before
  block i itself is quantized.
- w8a8: RTN weights plus simulated symmetric per-token quantization (at
  `act_bits`) of the input of every quantized linear, applied by permanent
  TransformerLens hooks on the quantized copy.

As in the feature metrics, the BOS position is excluded from calibration
statistics because its activations are outliers.
"""

from __future__ import annotations

import copy
from collections import defaultdict

import torch

# TransformerLens stores weights so that the layer computes x @ W.
# The input dimension is always axis -2, so per-output-channel scaling means
# taking the max over axis -2 (for every head / output column separately).
# `_W_K` / `_W_V` are the grouped-query attention names (e.g. Gemma 2) and
# `W_gate` is the gated-MLP input projection.
DEFAULT_TARGETS = ("W_Q", "W_K", "W_V", "_W_K", "_W_V", "W_O", "W_in", "W_gate", "W_out")
SCHEMES = ("rtn", "awq_lite", "awq_seq", "w8a8")

# Hook point (within blocks.{i}) holding the input of each target weight.
# W_O reads hook_z [batch, pos, n_heads, d_head], so its input channels are
# (head, d_head) pairs; all other inputs are [batch, pos, d_in].
INPUT_HOOKS = {
    "W_Q": "ln1.hook_normalized", "W_K": "ln1.hook_normalized", "W_V": "ln1.hook_normalized",
    "_W_K": "ln1.hook_normalized", "_W_V": "ln1.hook_normalized",
    "W_O": "attn.hook_z",
    "W_in": "ln2.hook_normalized", "W_gate": "ln2.hook_normalized",
    "W_out": "mlp.hook_post",
}


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


def _fq(w: torch.Tensor, bits: int, group_size: int | None, flat_in: bool) -> torch.Tensor:
    """fake_quantize, treating all axes but the last as one input axis when `flat_in`.

    TransformerLens stores W_O per head as [n_heads, d_head, d_model], but the
    deployed layer is one [n_heads * d_head, d_model] matrix: one scale per
    output channel (or group) across all heads.
    """
    if not flat_in:
        return fake_quantize(w, bits, group_size)
    return fake_quantize(w.reshape(-1, w.shape[-1]), bits, group_size).reshape(w.shape)


def fake_quantize_act(x: torch.Tensor, bits: int) -> torch.Tensor:
    """Symmetric per-token quantize-dequantize of activations.

    Args:
        x: [batch, pos, ...]; one scale per (batch, pos) over all remaining dims.
        bits: bit width. Values >= 16 return `x` unchanged.
    """
    if bits >= 16:
        return x.clone()
    if bits < 2:
        raise ValueError("bits must be >= 2")
    qmax = 2 ** (bits - 1) - 1
    x_f = x.float()
    amax = x_f.flatten(2).abs().amax(-1).reshape(*x.shape[:2], *[1] * (x.dim() - 2))
    scale = (amax / qmax).clamp(min=1e-12)
    return (torch.clamp(torch.round(x_f / scale), -qmax, qmax) * scale).to(x.dtype)


def _out_err(x: torch.Tensor, w: torch.Tensor, wq: torch.Tensor) -> float:
    """||x W - x Wq||^2 for x [rows, *in_shape]; W's input dims are axis -2 or all but -1."""
    d = (w - wq).float()
    if d.shape[:-1] == x.shape[1:]:  # W_O: input channel = (head, d_head)
        d = d.reshape(-1, d.shape[-1])
    else:  # [*heads, d_in, d_out] -> [d_in, heads * d_out]
        d = d.movedim(-2, 0).reshape(d.shape[-2], -1)
    return (x.reshape(x.shape[0], -1).float() @ d).pow(2).sum().item()


@torch.no_grad()
def awq_lite_weights(ws: list[torch.Tensor], x: torch.Tensor, bits: int,
                     group_size: int | None = None,
                     n_grid: int = 20) -> tuple[list[torch.Tensor], float]:
    """AWQ-style scaled quantization of weights that share one input.

    Args:
        ws: weights reading the same input; their input dims (axis -2, or all
            but the last axis for W_O) match `x.shape[1:]`.
        x: [rows, *in_shape] calibration inputs.
        n_grid: alpha is searched over n_grid + 1 points in [0, 1].
    Returns the quantize-dequantized weights (scale folded back in) and the
    chosen alpha, which minimises the output MSE summed over `ws`.
    """
    mean_abs = x.float().abs().mean(0)
    shape = (*mean_abs.shape, 1)
    best = (float("inf"), None, 0.0)
    for alpha in torch.linspace(0, 1, n_grid + 1).tolist():
        s = mean_abs.pow(alpha).clamp(min=1e-4)
        s = (s / (s.max() * s.min()).sqrt()).reshape(shape)
        qs = [(_fq(w.float() * s, bits, group_size, x.dim() > 2) / s).to(w.dtype) for w in ws]
        err = sum(_out_err(x, w, q) for w, q in zip(ws, qs))
        if err < best[0]:
            best = (err, qs, alpha)
    return best[1], best[2]


@torch.no_grad()
def _calib_inputs(model, tokens: torch.Tensor, hooks: list[str], batch_size: int,
                  max_rows: int = 1024) -> dict[str, torch.Tensor]:
    """Inputs at `hooks` on `tokens`, BOS dropped, strided subsample of <= max_rows rows."""
    n_rows = tokens.shape[0] * (tokens.shape[1] - 1)
    stride = -(-n_rows // max_rows)
    out = defaultdict(list)
    seen = 0
    for i in range(0, tokens.shape[0], batch_size):
        batch = tokens[i:i + batch_size].to(model.cfg.device)
        _, cache = model.run_with_cache(batch, names_filter=hooks)
        start = (-seen) % stride  # keep global rows 0, stride, 2*stride, ...
        for h in hooks:
            a = cache[h][:, 1:]
            out[h].append(a.reshape(-1, *a.shape[2:])[start::stride])
        seen += batch.shape[0] * (batch.shape[1] - 1)
    return {h: torch.cat(v) for h, v in out.items()}


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
        calib_tokens: [n, ctx] calibration tokens (BOS first), required by awq_lite / awq_seq.
        act_bits: activation bit width for w8a8.
        blocks: only quantize these transformer blocks (None = all).
        batch_size: batch size for the calibration forward passes.
    """
    if scheme not in SCHEMES:
        raise ValueError(f"unknown scheme {scheme!r}, expected one of {SCHEMES}")
    if scheme.startswith("awq") and calib_tokens is None:
        raise ValueError(f"{scheme} needs calib_tokens")
    qmodel = copy.deepcopy(model)
    params = {name: p for name, p in qmodel.named_parameters()
              if name.split(".")[-1] in targets and (blocks is None or block_of(name) in blocks)}
    if bits >= 16 and (scheme != "w8a8" or act_bits >= 16):
        return qmodel

    if not scheme.startswith("awq"):
        for name, p in params.items():
            p.copy_(_fq(p.data, bits, group_size, name.endswith("W_O")))
        if scheme == "w8a8" and act_bits < 16:
            def quant_act(x, hook):
                return fake_quantize_act(x, act_bits)
            for h in {_input_hook(n) for n in params}:
                qmodel.add_hook(h, quant_act, is_permanent=True)
        return qmodel

    groups = defaultdict(list)  # input hook -> params that read it
    for name, p in params.items():
        groups[_input_hook(name)].append(p)
    # awq_lite: one pass on the full-precision model for every layer.
    # awq_seq: one pass per block on qmodel, whose earlier blocks are quantized.
    # ponytail: each awq_seq pass runs the whole model (n_layers full forwards);
    # stop at block i if calibration time on deep models matters.
    seq = scheme == "awq_seq"
    stages = defaultdict(list)
    for h in groups:
        stages[block_of(h) if seq else 0].append(h)
    for _, hooks in sorted(stages.items()):
        xs = _calib_inputs(qmodel if seq else model, calib_tokens, hooks, batch_size)
        for h in hooks:
            qs, _ = awq_lite_weights([p.data for p in groups[h]], xs[h], bits, group_size)
            for p, q in zip(groups[h], qs):
                p.copy_(q)
    return qmodel


def _input_hook(name: str) -> str:
    """`blocks.3.attn.W_Q` -> `blocks.3.ln1.hook_normalized`."""
    return f"blocks.{block_of(name)}.{INPUT_HOOKS[name.split('.')[-1]]}"


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
