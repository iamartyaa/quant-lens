"""Streaming comparison of SAE feature activations between two models.

Sparse autoencoder dictionaries are large (GPT-2 small residual SAEs have
24,576 features), so holding [tokens x features] matrices for thousands of
tokens is expensive. `FeatureComparison` instead accumulates sufficient
statistics batch by batch and computes the final numbers at the end.

Definitions (a feature is "active" on a token when its activation is > 0):

- freq_ref / freq_q: fraction of tokens on which each feature is active.
- feature_corr: Pearson correlation of a feature's activations across tokens,
  reference vs quantized. Reported as the median over features that are alive
  in the reference model.
- lost_frac: of the features alive in the reference, the fraction that never
  fire in the quantized model.
- new_frac: features that never fire in the reference but are alive in the
  quantized model, as a fraction of reference-alive features.
- jaccard: per-token Jaccard similarity of the two sets of active features,
  averaged over tokens.
- topk_overlap: per-token overlap of the k most active features (k=10),
  averaged over tokens.
- fve_ref / fve_q: fraction of variance of the residual stream explained by
  the SAE reconstruction. A drop in fve_q means the SAE trained on the
  original model fits the quantized model's activations less well.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch


@dataclass
class ComparisonResult:
    n_tokens: int
    n_alive_ref: int
    median_feature_corr: float
    mean_feature_corr: float
    lost_frac: float
    new_frac: float
    jaccard: float
    topk_overlap: float
    l0_ref: float
    l0_q: float
    fve_ref: float
    fve_q: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class FeatureComparison:
    def __init__(self, d_sae: int, topk: int = 10, min_count: int = 5,
                 device: str | torch.device = "cpu"):
        self.d_sae = d_sae
        self.topk = topk
        self.min_count = min_count
        z = lambda: torch.zeros(d_sae, dtype=torch.float64, device=device)
        self.count_ref, self.count_q = z(), z()
        self.sx, self.sy, self.sxx, self.syy, self.sxy = z(), z(), z(), z(), z()
        self.n = 0
        self.jaccard_sum = 0.0
        self.topk_sum = 0.0
        self.l0_ref_sum = 0.0
        self.l0_q_sum = 0.0
        # residual-stream reconstruction stats: squared residual, plus the
        # running sums needed for exact total variance across batches
        self.fve_stats = {w: {"res": 0.0, "n": 0, "sum": None, "sumsq": 0.0} for w in ("ref", "q")}

    @torch.no_grad()
    def update(self, acts_ref: torch.Tensor, acts_q: torch.Tensor,
               x_ref: torch.Tensor | None = None, recon_ref: torch.Tensor | None = None,
               x_q: torch.Tensor | None = None, recon_q: torch.Tensor | None = None):
        """Add one batch.

        acts_*: [tokens, d_sae] SAE feature activations.
        x_*, recon_*: optional [tokens, d_model] activations and SAE
            reconstructions, for fraction-of-variance-explained.
        """
        a = acts_ref.double()
        b = acts_q.double()
        on_a = a > 0
        on_b = b > 0

        self.n += a.shape[0]
        self.count_ref += on_a.sum(0)
        self.count_q += on_b.sum(0)
        self.sx += a.sum(0)
        self.sy += b.sum(0)
        self.sxx += (a * a).sum(0)
        self.syy += (b * b).sum(0)
        self.sxy += (a * b).sum(0)

        inter = (on_a & on_b).sum(1).double()
        union = (on_a | on_b).sum(1).double()
        jac = torch.where(union > 0, inter / union.clamp(min=1), torch.ones_like(union))
        self.jaccard_sum += jac.sum().item()

        k = min(self.topk, self.d_sae)
        ta = a.topk(k, dim=1).indices
        tb = b.topk(k, dim=1).indices
        overlap = (ta.unsqueeze(2) == tb.unsqueeze(1)).any(2).sum(1).double() / k
        self.topk_sum += overlap.sum().item()

        self.l0_ref_sum += on_a.sum().item()
        self.l0_q_sum += on_b.sum().item()

        if x_ref is not None and recon_ref is not None:
            self._add_fve("ref", x_ref, recon_ref)
        if x_q is not None and recon_q is not None:
            self._add_fve("q", x_q, recon_q)

    def _add_fve(self, which: str, x: torch.Tensor, recon: torch.Tensor):
        x = x.double()
        s = self.fve_stats[which]
        s["res"] += (x - recon.double()).pow(2).sum().item()
        s["n"] += x.shape[0]
        s["sum"] = x.sum(0) if s["sum"] is None else s["sum"] + x.sum(0)
        s["sumsq"] += x.pow(2).sum().item()

    def _fve(self, which: str) -> float:
        s = self.fve_stats[which]
        if s["n"] == 0:
            return float("nan")
        tot = s["sumsq"] - s["sum"].pow(2).sum().item() / s["n"]
        return 1 - s["res"] / tot if tot > 0 else float("nan")

    def per_feature(self) -> dict[str, torch.Tensor]:
        """Per-feature freq_ref, mean_active_ref, freq_q and corr, as float CPU tensors."""
        n = max(self.n, 1)
        mx, my = self.sx / n, self.sy / n
        cov = self.sxy / n - mx * my
        vx = (self.sxx / n - mx * mx).clamp(min=0)
        vy = (self.syy / n - my * my).clamp(min=0)
        denom = (vx * vy).sqrt()
        corr = torch.where(denom > 0, cov / denom.clamp(min=1e-30),
                           torch.full_like(denom, float("nan")))
        return {
            "freq_ref": (self.count_ref / n).float().cpu(),
            # mean activation on the tokens where the feature fires (reference)
            "mean_active_ref": (self.sx / self.count_ref.clamp(min=1)).float().cpu(),
            "freq_q": (self.count_q / n).float().cpu(),
            "corr": corr.float().cpu(),
        }

    def result(self) -> ComparisonResult:
        pf = self.per_feature()
        alive_ref = self.count_ref.cpu() >= self.min_count
        alive_q = self.count_q.cpu() >= self.min_count
        n_alive = int(alive_ref.sum())
        corr_alive = pf["corr"][alive_ref]
        corr_alive = corr_alive[~torch.isnan(corr_alive)]
        lost = (alive_ref & (self.count_q.cpu() == 0)).sum().item()
        new = (alive_q & (self.count_ref.cpu() == 0)).sum().item()
        n = max(self.n, 1)
        return ComparisonResult(
            n_tokens=self.n,
            n_alive_ref=n_alive,
            median_feature_corr=corr_alive.median().item() if len(corr_alive) else float("nan"),
            mean_feature_corr=corr_alive.mean().item() if len(corr_alive) else float("nan"),
            lost_frac=lost / n_alive if n_alive else float("nan"),
            new_frac=new / n_alive if n_alive else float("nan"),
            jaccard=self.jaccard_sum / n,
            topk_overlap=self.topk_sum / n,
            l0_ref=self.l0_ref_sum / n,
            l0_q=self.l0_q_sum / n,
            fve_ref=self._fve("ref"),
            fve_q=self._fve("q"),
        )


@torch.no_grad()
def logit_stats(logits_ref: torch.Tensor, logits_q: torch.Tensor) -> tuple[float, float, int]:
    """Sum over tokens of KL(ref || q) and of top-1 agreement, plus token count."""
    lr = logits_ref.reshape(-1, logits_ref.shape[-1]).double().log_softmax(-1)
    lq = logits_q.reshape(-1, logits_q.shape[-1]).double().log_softmax(-1)
    kl = (lr.exp() * (lr - lq)).sum(-1)
    agree = (lr.argmax(-1) == lq.argmax(-1)).double()
    return kl.sum().item(), agree.sum().item(), lr.shape[0]
