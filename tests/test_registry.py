import dataclasses

import pytest

from quant_lens.registry import MODELS, ModelSpec, get_spec, resolve


def test_gpt2_matches_old_cli_defaults():
    s = resolve("gpt2")
    assert s.sae_release == "gpt2-small-res-jb"
    assert s.hook_template == "blocks.{layer}.hook_resid_pre"
    # the old CLI used the hook name as the SAE id
    assert s.sae_id_template.format(layer=6) == s.hook_template.format(layer=6)
    assert s.default_layers == (2, 6, 10)
    assert s.neuronpedia_template.format(layer=6, feature=7) == \
        "https://www.neuronpedia.org/gpt2-small/6-res-jb/7"


def test_gemma_spec():
    s = get_spec("gemma-2-2b")
    assert s.sae_release == "gemma-scope-2b-pt-res-canonical"
    assert s.hook_template.format(layer=12) == "blocks.12.hook_resid_post"
    assert s.sae_id_template.format(layer=12) == "layer_12/width_16k/canonical"
    assert s.default_layers == (5, 12, 19)


def test_spec_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        MODELS["gpt2"].sae_release = "x"


def test_overrides():
    s = resolve("gpt2", sae_release="other", layers=[1, 3])
    assert s.sae_release == "other" and s.default_layers == (1, 3)
    assert s.hook_template == MODELS["gpt2"].hook_template
    assert MODELS["gpt2"].sae_release == "gpt2-small-res-jb", "registry must not change"


def test_unknown_model():
    with pytest.raises(KeyError, match="gpt2"):
        get_spec("llama")
    with pytest.raises(ValueError, match="sae_id_template"):
        resolve("llama", sae_release="r", hook_template="h{layer}", layers=[0])
    s = resolve("llama", sae_release="r", hook_template="h{layer}", sae_id_template="s{layer}", layers=[0])
    assert s == ModelSpec("r", "h{layer}", "s{layer}", (0,), None)
