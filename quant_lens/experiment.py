"""Run the quantization-vs-features experiment.

For each bit width, build a quantized copy of the model, run the same tokens
through both models, encode the residual stream at each chosen layer with the
*same* pretrained SAE, and compare the resulting feature activations.

Optional analyses on top of the bit-width sweep, all at --analysis-bits:
--ablate-layers (quantize one block at a time), --fragility (which features
break) and --steering (do feature interventions transfer).

Example (GPU recommended, works on a free Colab T4):

    python -m quant_lens.experiment --model gpt2 --layers 2 6 10 \
        --bits 8 6 4 --scheme rtn --n-seqs 128 --ctx 128 --out results/
"""

from __future__ import annotations

import argparse
import json
import os
import time

import torch

from .metrics import FeatureComparison, logit_stats
from .quantize import quantize_model, weight_error


class SimpleSAE(torch.nn.Module):
    """Minimal ReLU sparse autoencoder with the same encode/decode interface
    as sae_lens SAEs. Used for offline tests; real runs load pretrained SAEs."""

    def __init__(self, d_model: int, d_sae: int, seed: int = 0):
        super().__init__()
        g = torch.Generator().manual_seed(seed)
        self.W_enc = torch.nn.Parameter(torch.randn(d_model, d_sae, generator=g) / d_model ** 0.5)
        self.b_enc = torch.nn.Parameter(torch.zeros(d_sae))
        self.W_dec = torch.nn.Parameter(self.W_enc.data.T.clone())
        self.b_dec = torch.nn.Parameter(torch.zeros(d_model))

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        """ReLU feature activations, [..., d_sae]."""
        return torch.relu((x - self.b_dec) @ self.W_enc + self.b_enc)

    def decode(self, f: torch.Tensor) -> torch.Tensor:
        """Reconstruction of the input from feature activations, [..., d_model]."""
        return f @ self.W_dec + self.b_dec


def load_pretrained_sae(release: str, sae_id: str, device: str) -> torch.nn.Module:
    """Load a pretrained SAE with sae_lens (handles old and new return types)."""
    from sae_lens import SAE

    out = SAE.from_pretrained(release=release, sae_id=sae_id, device=device)
    sae = out[0] if isinstance(out, tuple) else out
    sae.eval()
    return sae


def d_sae_of(sae) -> int:
    """Dictionary size of an SAE (sae_lens config or raw W_enc)."""
    if hasattr(sae, "cfg") and hasattr(sae.cfg, "d_sae"):
        return int(sae.cfg.d_sae)
    return int(sae.W_enc.shape[1])


def tokenize(model, texts: list[str], ctx: int, n_seqs: int) -> torch.Tensor:
    """Concatenate texts and cut into `n_seqs` sequences of `ctx` tokens,
    each starting with BOS."""
    bos = model.tokenizer.bos_token_id
    stream: list[int] = []
    for t in texts:
        stream.extend(model.tokenizer.encode(t))
        stream.append(model.tokenizer.eos_token_id)
        if len(stream) >= n_seqs * (ctx - 1):
            break
    body = ctx - 1
    n = min(n_seqs, len(stream) // body)
    if n == 0:
        raise ValueError("not enough text for one sequence")
    rows = [[bos] + stream[i * body:(i + 1) * body] for i in range(n)]
    return torch.tensor(rows, dtype=torch.long)


def load_texts(dataset: str | None, n_docs: int) -> list[str]:
    """First `n_docs` texts of a streaming HF dataset, or BUILTIN_TEXTS if None."""
    if dataset is None:
        return BUILTIN_TEXTS
    from datasets import load_dataset

    ds = load_dataset(dataset, split="train", streaming=True)
    texts = []
    for row in ds:
        texts.append(row["text"])
        if len(texts) >= n_docs:
            break
    return texts


@torch.no_grad()
def run_experiment(model, saes: dict[str, object], tokens: torch.Tensor, bits_list: list[int],
                   group_size: int | None = None, batch_size: int = 8, topk: int = 10,
                   min_count: int = 5, device: str = "cpu", per_feature_dir: str | None = None,
                   log=print, scheme: str = "rtn", calib_tokens: torch.Tensor | None = None,
                   act_bits: int = 8, blocks: list[int] | None = None) -> list[dict]:
    """Compare SAE features of `model` against quantized copies.

    Args:
        saes: mapping from hook name (e.g. "blocks.6.hook_resid_pre") to SAE.
        tokens: [n_seqs, ctx] token ids; position 0 (BOS) is excluded from
            feature statistics because its residual norm is an outlier.
        scheme, calib_tokens, act_bits, blocks: passed to `quantize_model`.
    Returns one summary row per (hook, bits).
    """
    hooks = list(saes)
    rows = []
    model.eval()
    for bits in bits_list:
        t0 = time.time()
        qmodel = quantize_model(model, bits, group_size, scheme=scheme, calib_tokens=calib_tokens,
                                act_bits=act_bits, blocks=blocks, batch_size=batch_size).eval()
        w_err = weight_error(model, qmodel)
        comps = {h: FeatureComparison(d_sae_of(saes[h]), topk, min_count, device) for h in hooks}
        kl_sum = agree_sum = n_tok = 0.0

        for i in range(0, tokens.shape[0], batch_size):
            batch = tokens[i:i + batch_size].to(device)
            logits_r, cache_r = model.run_with_cache(batch, names_filter=hooks)
            logits_q, cache_q = qmodel.run_with_cache(batch, names_filter=hooks)
            kl, agree, nt = logit_stats(logits_r[:, 1:], logits_q[:, 1:])
            kl_sum += kl
            agree_sum += agree
            n_tok += nt
            for h in hooks:
                sae = saes[h]
                xr = cache_r[h][:, 1:].reshape(-1, cache_r[h].shape[-1])
                xq = cache_q[h][:, 1:].reshape(-1, cache_q[h].shape[-1])
                fr = sae.encode(xr)
                fq = sae.encode(xq)
                comps[h].update(fr, fq, xr, sae.decode(fr), xq, sae.decode(fq))

        for h in hooks:
            res = comps[h].result().as_dict()
            row = {"hook": h, "bits": bits, "scheme": scheme, "group_size": group_size or 0,
                   "weight_rel_err": w_err, "logit_kl": kl_sum / max(n_tok, 1),
                   "top1_agree": agree_sum / max(n_tok, 1), **res}
            rows.append(row)
            log(f"[{bits}-bit] {h}: corr={res['median_feature_corr']:.3f} "
                f"jaccard={res['jaccard']:.3f} lost={res['lost_frac']:.3%} "
                f"fve {res['fve_ref']:.3f}->{res['fve_q']:.3f}")
            if per_feature_dir:
                os.makedirs(per_feature_dir, exist_ok=True)
                pf = comps[h].per_feature()
                torch.save({**pf, "n_tokens": comps[h].n},
                           os.path.join(per_feature_dir, f"{h}_{bits}bit.pt"))
        log(f"[{bits}-bit] weight rel err={w_err:.4f} KL={kl_sum / max(n_tok, 1):.4f} "
            f"top1={agree_sum / max(n_tok, 1):.3f} ({time.time() - t0:.0f}s)")
        del qmodel
    return rows


BUILTIN_TEXTS = [
    "The quick brown fox jumps over the lazy dog. Researchers study how neural networks represent concepts.",
    "In 1969, astronauts landed on the Moon. The mission was watched by millions of people around the world.",
    "def add(a, b):\n    return a + b\n\nprint(add(2, 3))",
    "Paris is the capital of France, and Berlin is the capital of Germany.",
    "She opened the door slowly, unsure of what she would find on the other side.",
    "Photosynthesis converts light energy into chemical energy stored in glucose.",
    "The stock market fell sharply on Monday after the central bank raised interest rates.",
    "To make tea, boil water, add the leaves, and let it steep for three minutes.",
] * 8


def run_suite(model, saes: dict[str, object], tokens: torch.Tensor, calib_tokens: torch.Tensor | None,
              args: argparse.Namespace, layers: dict[str, int],
              neuronpedia_template: str | None = None, device: str = "cpu",
              log=print) -> "pd.DataFrame":
    """Run the sweep plus any analyses requested in `args`, writing everything to args.out.

    Split out of `main` so the whole CLI path can be tested on a tiny model.
    `layers` maps each SAE hook to its layer number (used for Neuronpedia links).
    """
    import pandas as pd

    from .plots import plot_summary

    out = args.out
    os.makedirs(out, exist_ok=True)
    qkw = dict(group_size=args.group_size, scheme=args.scheme, calib_tokens=calib_tokens,
               act_bits=args.act_bits)
    per_feature_dir = (os.path.join(out, "per_feature")
                       if args.save_per_feature or args.fragility else None)
    rows = run_experiment(model, saes, tokens, args.bits, batch_size=args.batch_size, device=device,
                          per_feature_dir=per_feature_dir, log=log, **qkw)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(out, "summary.csv"), index=False)
    plot_summary(df, os.path.join(out, "summary.png"))
    with open(os.path.join(out, "config.json"), "w") as f:
        json.dump(vars(args), f, indent=2)
    log(f"wrote {out}/summary.csv and summary.png")

    bits = args.analysis_bits if args.analysis_bits is not None else min(args.bits)
    if args.ablate_layers:
        from .ablation import run_ablation, write_ablation

        abl = run_ablation(model, saes, tokens, bits, batch_size=args.batch_size, device=device,
                           log=log, **qkw)
        write_ablation(abl, out)
        log(f"wrote {out}/ablation.csv, ablation_table.csv, ablation_heatmap.png")
    if args.fragility:
        from .fragility import analyze_fragility

        if bits not in args.bits:
            raise ValueError(f"--analysis-bits {bits} must be one of --bits for --fragility")
        analyze_fragility(model, saes, tokens, per_feature_dir, bits, out, layers,
                          neuronpedia_template, n=args.fragility_n, batch_size=args.batch_size,
                          device=device)
        log(f"wrote {out}/fragile_features.csv, fragility_summary.csv, fragility_scatter.png")
    if args.steering:
        from .steering import plot_steering, run_steering

        steer_rows = []
        for b in sorted(set(args.bits) | {bits}, reverse=True):
            qmodel = quantize_model(model, b, batch_size=args.batch_size, **qkw).eval()
            for r in run_steering(model, qmodel, saes, tokens[:args.steer_seqs],
                                  n_features=args.steer_features, alphas=tuple(args.steer_alphas),
                                  batch_size=min(args.batch_size, 4), device=device, log=log):
                steer_rows.append({"bits": b, "scheme": args.scheme, **r})
            del qmodel
        sdf = pd.DataFrame(steer_rows)
        sdf.to_csv(os.path.join(out, "steering.csv"), index=False)
        plot_steering(sdf, os.path.join(out, "steering.png"))
        log(f"wrote {out}/steering.csv and steering.png")
    return df


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface; model-specific defaults come from the registry."""
    from .quantize import SCHEMES

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="gpt2", help="TransformerLens model name, e.g. gpt2 or gemma-2-2b")
    p.add_argument("--sae-release", default=None, help="SAELens release (default: from registry)")
    p.add_argument("--hook-template", default=None, help="e.g. blocks.{layer}.hook_resid_pre")
    p.add_argument("--sae-id-template", default=None, help="e.g. layer_{layer}/width_16k/canonical")
    p.add_argument("--layers", type=int, nargs="+", default=None)
    p.add_argument("--bits", type=int, nargs="+", default=[8, 6, 4])
    p.add_argument("--scheme", choices=SCHEMES, default="rtn")
    p.add_argument("--group-size", type=int, default=None)
    p.add_argument("--act-bits", type=int, default=8, help="activation bits for w8a8")
    p.add_argument("--calib-seqs", type=int, default=32,
                   help="calibration sequences for awq_lite, taken after the evaluation text")
    p.add_argument("--dataset", default="NeelNanda/pile-10k",
                   help="HF dataset with a 'text' column; 'builtin' for a tiny offline sample")
    p.add_argument("--n-docs", type=int, default=2000)
    p.add_argument("--n-seqs", type=int, default=128)
    p.add_argument("--ctx", type=int, default=128)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--out", default="results")
    p.add_argument("--save-per-feature", action="store_true")
    p.add_argument("--analysis-bits", type=int, default=None,
                   help="bit width for ablation/fragility (default: min of --bits)")
    p.add_argument("--ablate-layers", action="store_true",
                   help="quantize one block at a time and measure drift at every SAE hook")
    p.add_argument("--fragility", action="store_true", help="per-feature fragility analysis")
    p.add_argument("--fragility-n", type=int, default=50)
    p.add_argument("--steering", action="store_true", help="steering transfer analysis")
    p.add_argument("--steer-features", type=int, default=32)
    p.add_argument("--steer-alphas", type=float, nargs="+", default=[0.5],
                   help="steering strength in units of the mean residual norm at the hook")
    p.add_argument("--steer-seqs", type=int, default=16)
    return p


def main() -> None:
    """CLI entry point: load the model and SAEs, then `run_suite`."""
    args = build_parser().parse_args()

    from transformer_lens import HookedTransformer

    from .registry import resolve

    spec = resolve(args.model, args.sae_release, args.hook_template, args.sae_id_template, args.layers)
    args.sae_release, args.hook_template = spec.sae_release, spec.hook_template
    args.sae_id_template, args.layers = spec.sae_id_template, list(spec.default_layers)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(0)
    # fold_ln=False keeps LayerNorm gains out of W_Q/W_K/W_V/W_in, so we quantize the
    # weights a deployed model would quantize. Folding is exact, so the residual
    # stream the SAEs read is unchanged.
    model = HookedTransformer.from_pretrained(args.model, device=device, fold_ln=False)
    saes, layers = {}, {}
    for layer in args.layers:
        hook = args.hook_template.format(layer=layer)
        saes[hook] = load_pretrained_sae(args.sae_release, args.sae_id_template.format(layer=layer), device)
        layers[hook] = layer

    texts = load_texts(None if args.dataset == "builtin" else args.dataset, args.n_docs)
    all_tokens = tokenize(model, texts, args.ctx, args.n_seqs + args.calib_seqs)
    tokens, calib = all_tokens[:args.n_seqs], all_tokens[args.n_seqs:]
    if args.scheme == "awq_lite" and len(calib) == 0:
        raise ValueError("not enough text for calibration; raise --n-docs or lower --n-seqs")
    print(f"{tokens.shape[0]} sequences x {tokens.shape[1]} tokens on {device} "
          f"({calib.shape[0]} calibration sequences)")
    run_suite(model, saes, tokens, calib, args, layers, spec.neuronpedia_template, device)


if __name__ == "__main__":
    main()
