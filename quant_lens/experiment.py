"""Run the quantization-vs-features experiment.

For each bit width, build a quantized copy of the model, run the same tokens
through both models, encode the residual stream at each chosen layer with the
*same* pretrained SAE, and compare the resulting feature activations.

Example (GPU recommended, works on a free Colab T4):

    python -m quant_lens.experiment --model gpt2 \
        --sae-release gpt2-small-res-jb --layers 2 6 10 \
        --bits 8 6 4 --n-seqs 128 --ctx 128 --out results/
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

    def encode(self, x):
        return torch.relu((x - self.b_dec) @ self.W_enc + self.b_enc)

    def decode(self, f):
        return f @ self.W_dec + self.b_dec


def load_pretrained_sae(release: str, sae_id: str, device: str):
    """Load a pretrained SAE with sae_lens (handles old and new return types)."""
    from sae_lens import SAE

    out = SAE.from_pretrained(release=release, sae_id=sae_id, device=device)
    sae = out[0] if isinstance(out, tuple) else out
    sae.eval()
    return sae


def d_sae_of(sae) -> int:
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
                   log=print) -> list[dict]:
    """Compare SAE features of `model` against quantized copies.

    Args:
        saes: mapping from hook name (e.g. "blocks.6.hook_resid_pre") to SAE.
        tokens: [n_seqs, ctx] token ids; position 0 (BOS) is excluded from
            feature statistics because its residual norm is an outlier.
    Returns one summary row per (hook, bits).
    """
    hooks = list(saes)
    rows = []
    model.eval()
    for bits in bits_list:
        t0 = time.time()
        qmodel = quantize_model(model, bits, group_size).eval()
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
            row = {"hook": h, "bits": bits, "group_size": group_size or 0,
                   "weight_rel_err": w_err, "logit_kl": kl_sum / max(n_tok, 1),
                   "top1_agree": agree_sum / max(n_tok, 1), **res}
            rows.append(row)
            log(f"[{bits}-bit] {h}: corr={res['median_feature_corr']:.3f} "
                f"jaccard={res['jaccard']:.3f} lost={res['lost_frac']:.3%} "
                f"fve {res['fve_ref']:.3f}->{res['fve_q']:.3f}")
            if per_feature_dir:
                os.makedirs(per_feature_dir, exist_ok=True)
                pf = comps[h].per_feature()
                torch.save(pf, os.path.join(per_feature_dir, f"{h}_{bits}bit.pt"))
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="gpt2")
    p.add_argument("--sae-release", default="gpt2-small-res-jb")
    p.add_argument("--hook-template", default="blocks.{layer}.hook_resid_pre")
    p.add_argument("--layers", type=int, nargs="+", default=[2, 6, 10])
    p.add_argument("--bits", type=int, nargs="+", default=[8, 6, 4])
    p.add_argument("--group-size", type=int, default=None)
    p.add_argument("--dataset", default="NeelNanda/pile-10k",
                   help="HF dataset with a 'text' column; 'builtin' for a tiny offline sample")
    p.add_argument("--n-docs", type=int, default=2000)
    p.add_argument("--n-seqs", type=int, default=128)
    p.add_argument("--ctx", type=int, default=128)
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--out", default="results")
    p.add_argument("--save-per-feature", action="store_true")
    args = p.parse_args()

    from transformer_lens import HookedTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    torch.manual_seed(0)
    model = HookedTransformer.from_pretrained(args.model, device=device)
    saes = {}
    for layer in args.layers:
        hook = args.hook_template.format(layer=layer)
        saes[hook] = load_pretrained_sae(args.sae_release, hook, device)

    texts = load_texts(None if args.dataset == "builtin" else args.dataset, args.n_docs)
    tokens = tokenize(model, texts, args.ctx, args.n_seqs)
    print(f"{tokens.shape[0]} sequences x {tokens.shape[1]} tokens on {device}")

    os.makedirs(args.out, exist_ok=True)
    rows = run_experiment(model, saes, tokens, args.bits, args.group_size, args.batch_size,
                          device=device,
                          per_feature_dir=os.path.join(args.out, "per_feature") if args.save_per_feature else None)

    import pandas as pd

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(args.out, "summary.csv"), index=False)
    with open(os.path.join(args.out, "config.json"), "w") as f:
        json.dump(vars(args), f, indent=2)
    from .plots import plot_summary

    plot_summary(df, os.path.join(args.out, "summary.png"))
    print(f"wrote {args.out}/summary.csv and summary.png")


if __name__ == "__main__":
    main()
