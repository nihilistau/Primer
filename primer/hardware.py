"""Detect GPU/RAM and suggest a starting tier."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Hardware:
    gpu_name: str | None
    vram_gb: float
    ram_gb: float


def _ram_gb() -> float:
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1024**3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (OSError, ValueError, AttributeError):
        return 0.0


def _gpu() -> tuple[str | None, float]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, 0.0
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        name, mem = out.strip().splitlines()[0].rsplit(",", 1)
        return name.strip(), float(mem) / 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, 0.0


def detect_hardware() -> Hardware:
    name, vram = _gpu()
    return Hardware(name, vram, _ram_gb())


def suggest_tier(hw: Hardware) -> str:
    if hw.vram_gb >= 20:
        return "workstation"
    if hw.vram_gb >= 8:
        return "hybrid"
    return "light"
