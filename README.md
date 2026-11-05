# quant-lens

**Does quantizing a model's weights change what it represents inside?**

Quantization is evaluated almost entirely from the outside: perplexity, benchmark scores, maybe KL divergence on the logits. But a model can keep its outputs close while the internal computation drifts. If interpretability tools are going to be used to audit models that are actually deployed, and deployed models are usually quantized, we need to know whether the features found in the full-precision model still describe the quantized one.

This project measures that directly. It takes a model, quantizes its weights at several bit widths, runs the same text through both versions, and encodes the residual stream at chosen layers with the **same pretrained sparse autoencoder (SAE)**. Then it compares the feature activations token by token.

> **Status: work in progress.** The pipeline and tests are done. First results on GPT-2 small are next.

## Questions

1. How many bits can you remove before SAE features stop matching? Is there a cliff?
2. Which layers drift first?
3. Are some features fragile, for example rare features or ones with small activations?
4. Do features drift before the output does? Feature metrics are compared against logit KL and top-1 agreement on the same tokens.

## Method

- **Model and SAEs:** GPT-2 small with the public residual-stream SAEs `gpt2-small-res-jb` (24,576 features per layer), loaded through [SAELens](https://github.com/jbloomAus/SAELens) and [TransformerLens](https://github.com/TransformerLensOrg/TransformerLens).
- **Quantization:** weight-only, symmetric round-to-nearest, with scales per output channel or per group (`--group-size`). It uses simulated quantization (quantize, then dequantize), so the measurement isolates the rounding itself rather than differences between low-bit kernels. Attention and MLP weights are quantized. Embeddings, unembedding, biases and LayerNorm stay in full precision, as in common practice.
- **Metrics,** computed in a streaming way so thousands of tokens fit in memory:

| metric | meaning |
|---|---|
| `median_feature_corr` | Pearson correlation of each feature's activations across tokens, reference vs quantized, as the median over live features |
| `jaccard` | per-token overlap of the sets of active features |
| `topk_overlap` | per-token overlap of the 10 strongest features |
| `lost_frac` / `new_frac` | features that stop firing entirely, or start firing only after quantization |
| `fve_ref` / `fve_q` | how much of the residual stream the SAE explains, before and after |
| `logit_kl`, `top1_agree` | output-level drift on the same tokens, for comparison |

The BOS position is excluded from feature statistics because its residual norm is an outlier.

## Run it

On a free Colab GPU, open `notebooks/run_gpt2.ipynb`. Or locally:

```bash
pip install -r requirements.txt
python -m quant_lens.experiment --model gpt2 --sae-release gpt2-small-res-jb \
    --layers 2 6 10 --bits 8 6 4 3 --n-seqs 128 --ctx 128 --out results/
```

This writes `results/summary.csv` and `results/summary.png`. Add `--save-per-feature` to keep per-feature frequencies and correlations for deeper analysis.

Tests run offline on a tiny random model:

```bash
pytest tests
```

## Layout

```
quant_lens/quantize.py    simulated weight quantization for TransformerLens models
quant_lens/metrics.py     streaming SAE feature comparison + logit drift
quant_lens/experiment.py  experiment loop and CLI
quant_lens/plots.py       summary plot
tests/                    offline tests (quantization, metrics, end-to-end)
```

## Next steps

- First full run on GPT-2 small across layers and bit widths
- Per-feature analysis: which features break first, and is it related to activation frequency or magnitude?
- Group-wise vs per-channel quantization
- Repeat on a larger model with public SAEs (e.g. Gemma 2 2B with Gemma Scope)
