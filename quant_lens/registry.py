"""Known models and the SAE releases, hook names and default layers to use with them.

Templates are formatted with `layer=` (and `feature=` for Neuronpedia links).
Values were checked against SAELens `pretrained_saes.yaml`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    sae_release: str
    hook_template: str
    sae_id_template: str
    default_layers: tuple[int, ...]
    neuronpedia_template: str | None = None


MODELS: dict[str, ModelSpec] = {
    "gpt2": ModelSpec(
        sae_release="gpt2-small-res-jb",
        hook_template="blocks.{layer}.hook_resid_pre",
        sae_id_template="blocks.{layer}.hook_resid_pre",
        default_layers=(2, 6, 10),
        neuronpedia_template="https://www.neuronpedia.org/gpt2-small/{layer}-res-jb/{feature}",
    ),
    "gemma-2-2b": ModelSpec(
        sae_release="gemma-scope-2b-pt-res-canonical",
        hook_template="blocks.{layer}.hook_resid_post",
        sae_id_template="layer_{layer}/width_16k/canonical",
        default_layers=(5, 12, 19),
        neuronpedia_template="https://www.neuronpedia.org/gemma-2-2b/{layer}-gemmascope-res-16k/{feature}",
    ),
}


def get_spec(model_name: str) -> ModelSpec:
    """Registry entry for `model_name`; KeyError naming the known models otherwise."""
    try:
        return MODELS[model_name]
    except KeyError:
        raise KeyError(f"unknown model {model_name!r}; known models: {', '.join(MODELS)}") from None


def resolve(model_name: str, sae_release: str | None = None, hook_template: str | None = None,
            sae_id_template: str | None = None, layers: list[int] | tuple[int, ...] | None = None
            ) -> ModelSpec:
    """Registry values for `model_name`, with any explicit argument taking precedence.

    For a model not in the registry, all four of sae_release, hook_template,
    sae_id_template and layers must be given (ValueError otherwise), and the
    Neuronpedia template is None.
    """
    base = MODELS.get(model_name)
    if base is None:
        missing = [k for k, v in (("sae_release", sae_release), ("hook_template", hook_template),
                                  ("sae_id_template", sae_id_template), ("layers", layers)) if v is None]
        if missing:
            raise ValueError(f"model {model_name!r} is not in the registry ({', '.join(MODELS)}); "
                             f"pass {', '.join(missing)} explicitly")
        base = ModelSpec("", "", "", ())
    return ModelSpec(
        sae_release=sae_release or base.sae_release,
        hook_template=hook_template or base.hook_template,
        sae_id_template=sae_id_template or base.sae_id_template,
        default_layers=tuple(layers) if layers is not None else base.default_layers,
        neuronpedia_template=base.neuronpedia_template,
    )
