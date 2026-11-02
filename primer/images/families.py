"""Image-model families: the files each needs, sensible defaults, size rules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_W = (".safetensors", ".gguf")


@dataclass(frozen=True)
class Slot:
    name: str
    flag: str
    exts: tuple[str, ...]
    required: bool


@dataclass(frozen=True)
class Family:
    id: str
    label: str
    recipe: str
    slots: tuple[Slot, ...]
    defaults: dict
    size_multiple: int
    prompt_style: Literal["tags", "prose"]


FAMILIES: dict[str, Family] = {
    "sdxl": Family(
        id="sdxl", label="SDXL", recipe="sdxl-checkpoint",
        slots=(Slot("model", "-m", _W, True),
               Slot("vae", "--vae", _W, False)),
        defaults={"width": 1024, "height": 1024, "steps": 20, "cfg": 6,
                  "sampler": "euler_a", "vae_tiling": True, "offload": False},
        size_multiple=8, prompt_style="tags"),
    "qwen-image-2.1": Family(
        id="qwen-image-2.1", label="Qwen-Image 2.1", recipe="qwen-image-21",
        slots=(Slot("diffusion", "--diffusion-model", (".gguf", ".safetensors"), True),
               Slot("llm", "--llm", (".gguf", ".safetensors"), True),
               Slot("vae", "--vae", (".safetensors",), True)),
        defaults={"width": 1024, "height": 1024, "steps": 20, "cfg": 4,
                  "sampler": "euler", "offload": True},
        size_multiple=32, prompt_style="prose"),
    "moss-image": Family(
        id="moss-image", label="MOSS-Image", recipe="moss-image-sft",
        slots=(Slot("diffusion", "--diffusion-model", _W, True),
               Slot("llm", "--llm", _W, True),
               Slot("vae", "--vae", _W, True),
               Slot("tokenizer", "--tokenizer", (".json",), True)),
        defaults={"width": 1024, "height": 1024, "steps": 20, "cfg": 3,
                  "sampler": "euler", "flow_shift": 5, "offload": True},
        size_multiple=16, prompt_style="prose"),
}

SAMPLERS = ("euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "lcm")

# name -> (type, lo, hi); sampler's lo is its allowed values, negative's hi a max length
SETTINGS: dict[str, tuple[type, object, object]] = {
    "width": (int, 256, 2048),
    "height": (int, 256, 2048),
    "steps": (int, 1, 150),
    "cfg": (float, 0, 30),
    "sampler": (str, SAMPLERS, None),
    "seed": (int, -1, 2**31 - 1),
    "negative": (str, 0, 1000),
    "offload": (bool, None, None),
    "vae_tiling": (bool, None, None),
    "flash_attn": (bool, None, None),
    "flow_shift": (float, 0, 20),  # None allowed
}
