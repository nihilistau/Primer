"""Image- and video-model families: the files each needs, sensible defaults, size rules."""
from __future__ import annotations

from dataclasses import dataclass, field
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
    prompt_style: Literal["tags", "prose", "motion"]
    kind: Literal["image", "video"] = "image"
    needs_init: bool = False  # image-to-video only: a start image is required
    frame_step: int = 0  # frames are a multiple of this plus one (4 Wan, 8 LTX); 0 for images
    fast_defaults: dict = field(default_factory=dict)  # applied when a few-step LoRA is attached
    can_animate: bool = False  # takes a start image (Animate); needs_init families must too

    @property
    def two_stage(self) -> bool:
        """A high-noise and a low-noise model (Wan 2.2 A14B): LoRAs can target either stage."""
        return any(s.name == "high_noise" for s in self.slots)


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

_WAN_A14B_SLOTS = (Slot("diffusion", "--diffusion-model", _W, True),  # the low-noise model
                   Slot("high_noise", "--high-noise-diffusion-model", _W, True),
                   Slot("t5xxl", "--t5xxl", _W, True),
                   Slot("vae", "--vae", _W, True))
_WAN_A14B_DEFAULTS = {"width": 832, "height": 480, "frames": 33, "fps": 16, "steps": 20, "cfg": 3.5,
                      "sampler": "euler",
                      "high_noise_steps": -1, "high_noise_cfg": 3.5, "moe_boundary": 0.875,
                      "flow_shift": 5, "offload": True, "vae_tiling": True}
_WAN_A14B_FAST = {"steps": 4, "high_noise_steps": 2, "cfg": 1, "high_noise_cfg": 1}
_LTX_SLOTS = (Slot("diffusion", "--diffusion-model", _W, True),
              Slot("llm", "--llm", _W, True),
              Slot("connectors", "--embeddings-connectors", _W, True),
              Slot("vae", "--vae", _W, True))
_LTX_DEFAULTS = {"width": 768, "height": 512, "frames": 49, "fps": 24, "steps": 8, "cfg": 1,
                 "sampler": "euler", "offload": True, "vae_tiling": True}

FAMILIES.update({
    "wan2.2-ti2v-5b": Family(
        id="wan2.2-ti2v-5b", can_animate=True, label="Wan 2.2 TI2V 5B", recipe="wan22-ti2v-5b",
        slots=(Slot("diffusion", "--diffusion-model", _W, True),
               Slot("t5xxl", "--t5xxl", _W, True),
               Slot("vae", "--vae", _W, True)),
        defaults={"width": 832, "height": 480, "frames": 49, "fps": 24, "steps": 20, "cfg": 5,
                  "sampler": "euler", "flow_shift": 5, "offload": True, "vae_tiling": True},
        size_multiple=32, prompt_style="motion", kind="video", frame_step=4,
        fast_defaults={}),  # Kijai Turbo LoRA at 8 steps/cfg 1 ignored the prompt (measured)
    "wan2.2-t2v-a14b": Family(
        id="wan2.2-t2v-a14b", label="Wan 2.2 T2V A14B", recipe="wan22-t2v",
        slots=_WAN_A14B_SLOTS, defaults=dict(_WAN_A14B_DEFAULTS),
        size_multiple=16, prompt_style="motion", kind="video", frame_step=4,
        fast_defaults=dict(_WAN_A14B_FAST)),
    "wan2.2-i2v-a14b": Family(
        id="wan2.2-i2v-a14b", can_animate=True, label="Wan 2.2 I2V A14B", recipe="wan22-i2v",
        slots=_WAN_A14B_SLOTS, defaults={**_WAN_A14B_DEFAULTS, "moe_boundary": 0.9},
        size_multiple=16, prompt_style="motion", kind="video", needs_init=True, frame_step=4,
        fast_defaults=dict(_WAN_A14B_FAST)),
    "ltx-2.3-distilled": Family(
        id="ltx-2.3-distilled", can_animate=True, label="LTX-2.3 distilled", recipe="ltx23-distilled",
        slots=_LTX_SLOTS, defaults=dict(_LTX_DEFAULTS),
        size_multiple=32, prompt_style="motion", kind="video", frame_step=8),
    "ltx-2.3": Family(
        id="ltx-2.3", can_animate=True, label="LTX-2.3 dev", recipe="ltx23-dev",
        slots=_LTX_SLOTS, defaults={**_LTX_DEFAULTS, "steps": 20, "cfg": 3},
        size_multiple=32, prompt_style="motion", kind="video", frame_step=8),
})

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

# request-time video settings: valid only for video families (image families drop them as
# unknown), and the high_noise_*/moe_boundary ones only for two-stage families
VIDEO_SETTINGS: dict[str, tuple[type, object, object]] = {
    "frames": (int, 1, 241),  # then aligned down to the family's frame_step
    "fps": (int, 1, 60),
    "high_noise_steps": (int, -1, 150),  # -1: let sd-server choose
    "high_noise_cfg": (float, 0, 30),
    "moe_boundary": (float, 0, 1),
}
TWO_STAGE_SETTINGS = ("high_noise_steps", "high_noise_cfg", "moe_boundary")
VIDEO_MAX_SIZE = 1280  # width and height for video families; images keep SETTINGS' 2048
SETTINGS.update(VIDEO_SETTINGS)
