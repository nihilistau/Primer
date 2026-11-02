import shutil

import pytest

from primer.hardware import Hardware, detect_hardware, suggest_tier


@pytest.mark.parametrize("vram,expected", [(0, "light"), (6, "light"), (8, "hybrid"), (12, "hybrid"), (24, "workstation"), (48, "workstation")])
def test_suggest_tier(vram, expected):
    assert suggest_tier(Hardware("gpu" if vram else None, vram, 32)) == expected


def test_detect_hardware_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    hw = detect_hardware()
    assert hw.gpu_name is None and hw.vram_gb == 0 and hw.ram_gb > 0
