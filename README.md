# quant-lens

**Does quantizing a model's weights change what it represents inside?**

Quantization is evaluated almost entirely from the outside: perplexity, benchmark scores, maybe KL divergence on the logits. But a model can keep its outputs close while the internal computation drifts. If interpretability tools are going to be used to audit models that are actually deployed, and deployed models are usually quantized, we need to know whether the features found in the full-precision model still describe the quantized one.

This project measures that directly. It takes a model, quantizes it at several bit widths, runs the same text through both versions, and encodes the residual stream at chosen layers with the **same pretrained sparse autoencoder (SAE)**. Then it compares the feature activations token by token, and goes further: which block causes the drift, which features break, and whether steering with a feature still does the same thing after quantization.

> **Status: work in progress.** The pipeline, the analyses and the tests are done. No full run has been made yet.

## Questions

1. How many bits can you remove before SAE features stop matching? Is there a cliff?
2. Which layers drift first, and which block is responsible when they do?
3. Are some features fragile, for example rare features, ones with small activations, or ones with small decoder norms?
4. Do features drift before the output does? Feature metrics are compared against logit KL and top-1 agreement on the same tokens.
5. Does a smarter quantization scheme (activation-aware scaling) or quantizing activations as well change the picture?
6. If you steer the model along a feature direction, does the quantized model respond the same way?

## Method

### Models and SAEs

Models run in [TransformerLens](https://github.com/TransformerLensOrg/TransformerLens) as `HookedTransformer`s, and SAEs are loaded through [SAELens](https://github.com/jbloomAus/SAELens). A small registry (`quant_lens/registry.py`) maps a model name to its SAE release, hook name, SAE id, default layers and Neuronpedia link, so `--model` is usually all you need:

| `--model` | SAE release | hook | SAE id | default layers |
|---|---|---|---|---|
| `gpt2` | `gpt2-small-res-jb` | `blocks.{layer}.hook_resid_pre` | same as the hook | 2 6 10 |
| `gemma-2-2b` | `gemma-scope-2b-pt-res-canonical` | `blocks.{layer}.hook_resid_post` | `layer_{layer}/width_16k/canonical` | 5 12 19 |

Any other TransformerLens model works by passing `--sae-release`, `--hook-template` and `--sae-id-template` yourself. Gemma 2 is gated on Hugging Face, so accept its license and log in first.

### Quantization schemes

All quantization is **simulated** (quantize, then dequantize back to float), so the measurement isolates the rounding itself rather than differences between low-bit kernels. Attention and MLP weights are quantized. Embeddings, unembedding, biases and LayerNorm stay in full precision, as in common practice. The original model is never modified, so one reference is compared against every bit width.

- **`rtn`**: symmetric round-to-nearest weights, with one scale per output channel, or per group of `--group-size` input rows.
- **`awq_lite`**: a stripped-down version of activation-aware weight quantization. On a held-out calibration set (`--calib-seqs` sequences, BOS excluded), it measures the mean absolute activation of every input channel of each linear layer. Salient channels are scaled up by `s = mean_abs ** alpha` before rounding and the scale is divided back out afterwards (`Q(W * s) / s`, equivalent to folding `1/s` into the previous op). `alpha` is chosen per layer by a grid search over [0, 1] that minimizes the layer's output MSE. Q, K and V share one scale, as do `W_in` and `W_gate`, as in AWQ. There is no clipping search, and calibration uses full-precision activations. Calibration text is taken from after the evaluation sequences, so the two never overlap.
- **`w8a8`**: RTN weights plus simulated symmetric per-token activation quantization (`--act-bits`, default 8) at the inputs of the quantized linear layers, applied through TransformerLens hooks.

### Measurement

The BOS position is excluded from every feature statistic, because its residual norm is an outlier. Metrics are computed in a streaming way, so thousands of tokens fit in memory even with tens of thousands of SAE features.

## Metrics

A feature is *active* on a token when its activation is above 0, and *live* when it fires on at least a handful of reference tokens.

### Main sweep (`summary.csv`, one row per hook and bit width)

| metric | meaning |
|---|---|
| `median_feature_corr` | Pearson correlation of each feature's activations across tokens, reference vs quantized, as the median over live features |
| `mean_feature_corr` | the same correlation, averaged instead of median |
| `jaccard` | per-token overlap of the sets of active features, averaged over tokens |
| `topk_overlap` | per-token overlap of the 10 strongest features |
| `lost_frac` | of the reference-live features, the fraction that never fire in the quantized model |
| `new_frac` | features that never fire in the reference but are live in the quantized model, as a fraction of reference-live features |
| `l0_ref` / `l0_q` | mean number of active features per token, before and after |
| `fve_ref` / `fve_q` | fraction of residual-stream variance the SAE reconstruction explains, before and after. A drop in `fve_q` means the SAE fits the quantized model's activations less well |
| `weight_rel_err` | relative Frobenius error of the quantized weights, pooled over all quantized matrices |
| `logit_kl` | KL(reference ‖ quantized) of the next-token distribution, averaged over tokens |
| `top1_agree` | fraction of tokens where both models predict the same next token |

### Layer ablation (`--ablate-layers`)

| output | meaning |
|---|---|
| `ablation_table.csv` | `median_feature_corr` for every pair of [quantized block × measured hook]. Only one block is quantized at a time, at `--analysis-bits`; the rest stay in full precision. Hooks upstream of the quantized block match exactly by construction, the first hook after it shows that block's own effect, and later hooks show whether the error grows or heals |
| `ablation.csv` | the same runs in long form, with every main-sweep metric |

### Feature fragility (`--fragility`)

| output | meaning |
|---|---|
| `fragile_features.csv` | the 50 most fragile (lowest correlation) and 50 most robust (highest correlation) live features per layer at `--analysis-bits`, with `corr`, `freq_ref`, `freq_q`, `mean_active_ref` (mean reference activation when active), `dec_norm` (decoder-row norm), the top-activating reference contexts and a Neuronpedia link |
| `fragility_summary.csv` | per layer, the Spearman rank correlation of feature correlation against log10 reference firing frequency (`rho_log10_freq_ref`), mean activation when active (`rho_mean_active_ref`) and decoder norm (`rho_dec_norm`) |

### Steering transfer (`--steering`)

For a sample of live features (`--steer-features`), add `alpha * r * d` at every non-BOS position, where `d` is the feature's unit decoder direction and `r` is the mean reference residual norm at that hook (excluding BOS), in both models. Then compare how next-token log-probs move. `alpha` is set with `--steer-alphas` on `--steer-seqs` sequences.

| metric | meaning |
|---|---|
| `cos_mean` | mean per-token cosine between the reference and quantized log-prob deltas (steered minus clean). 1 means steering moves both models the same way |
| `kl_steered` | KL(reference steered ‖ quantized steered) |
| `kl_clean` | KL(reference clean ‖ quantized clean), the unsteered baseline |
| `delta_norm_ref` / `delta_norm_q` | size of the log-prob change caused by steering, per model |
| `effect_ratio` | `delta_norm_q / delta_norm_ref`: above 1 steering hits harder after quantization, below 1 it is weaker |

## Run it

On Colab, open `notebooks/run_gpt2.ipynb` (T4 GPU) or `notebooks/run_gemma.ipynb` (A100; the float32 reference plus a quantized copy take roughly 21 GB). Locally:

```bash
pip install -e .
```

Main sweep on GPT-2 small, with the registry defaults (layers 2, 6, 10):

```bash
python -m quant_lens.experiment --model gpt2 --scheme rtn --bits 8 6 4 3 --out results/gpt2_rtn
```

Other schemes:

```bash
python -m quant_lens.experiment --model gpt2 --scheme rtn --group-size 128 --bits 8 6 4 3 --out results/gpt2_rtn_g128
python -m quant_lens.experiment --model gpt2 --scheme awq_lite --calib-seqs 32 --bits 8 6 4 3 --out results/gpt2_awq_lite
python -m quant_lens.experiment --model gpt2 --scheme w8a8 --act-bits 8 --bits 8 6 4 3 --out results/gpt2_w8a8
```

The analyses run at `--analysis-bits` (default: the lowest value in `--bits`) and can be combined in one run. Layer ablation is most useful with every layer measured:

```bash
# which block causes the drift
python -m quant_lens.experiment --model gpt2 --layers 0 1 2 3 4 5 6 7 8 9 10 11 \
    --bits 8 6 4 3 --ablate-layers --out results/gpt2_rtn

# which features break, and what they have in common
python -m quant_lens.experiment --model gpt2 --bits 8 6 4 3 --fragility --fragility-n 50 --out results/gpt2_rtn

# does feature steering transfer
python -m quant_lens.experiment --model gpt2 --bits 8 6 4 3 --steering \
    --steer-features 32 --steer-alphas 0.5 --steer-seqs 16 --out results/gpt2_rtn
```

Gemma 2 2B with Gemma Scope (registry defaults, layers 5, 12, 19), with a smaller batch to fit memory:

```bash
python -m quant_lens.experiment --model gemma-2-2b --scheme rtn --bits 8 6 4 3 \
    --n-seqs 64 --batch-size 2 --ablate-layers --fragility --steering --out results/gemma_rtn
```

Then write a report for any run directory:

```bash
python -m quant_lens.report results/gpt2_rtn
```

Other useful options: `--dataset` (default `NeelNanda/pile-10k`; `builtin` uses a tiny offline sample), `--n-docs`, `--n-seqs` (default 128), `--ctx` (default 128), `--batch-size` (default 8), `--save-per-feature`.

## Outputs

Everything goes into `--out`:

| file | written when |
|---|---|
| `summary.csv`, `summary.png`, `config.json` | always |
| `per_feature/{hook}_{bits}bit.pt` | `--save-per-feature` or `--fragility` |
| `ablation.csv`, `ablation_table.csv`, `ablation_heatmap.png` | `--ablate-layers` |
| `fragile_features.csv`, `fragility_summary.csv`, `fragility_scatter.png` | `--fragility` |
| `steering.csv`, `steering.png` | `--steering` |
| `report.md` | `python -m quant_lens.report <dir>` |

## Tests

The tests run offline on a tiny random model with random SAEs, so they need no GPU, downloads or Hugging Face token:

```bash
pip install -e ".[dev]"
pytest tests
```

GitHub Actions runs them on every push and pull request (`.github/workflows/tests.yml`).

## Layout

```
quant_lens/quantize.py    simulated quantization: rtn, awq_lite, w8a8
quant_lens/metrics.py     streaming SAE feature comparison + logit drift
quant_lens/experiment.py  experiment loop and CLI
quant_lens/registry.py    model name -> SAE release, hooks, default layers, Neuronpedia link
quant_lens/ablation.py    quantize one block at a time, measure drift at every hook
quant_lens/fragility.py   most fragile / robust features and what predicts fragility
quant_lens/steering.py    does steering along a feature direction survive quantization
quant_lens/report.py      report.md from a results directory
quant_lens/plots.py       summary plot
notebooks/                Colab notebooks for GPT-2 small (T4) and Gemma 2 2B (A100)
tests/                    offline tests on a tiny random model
```

## Results

Results will be added here after the first full run.

## Roadmap

- First full run on GPT-2 small: all three schemes, the group-size variant, and the analyses on RTN
- The same on Gemma 2 2B with Gemma Scope
- Compare feature drift against output drift across schemes, to see which one keeps the internals closest at a given bit width
- Look at the fragile features by hand through their contexts and Neuronpedia dashboards
- Real low-bit kernels (for example GPTQ or bitsandbytes checkpoints) alongside simulated quantization, to check the two agree
