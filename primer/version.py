"""Primer's version, read once from pyproject.toml (the release step bumps it there)."""
from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def read_version(path: Path = PYPROJECT) -> str:
    try:
        version = tomllib.loads(Path(path).read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return "dev"
    return version if isinstance(version, str) and version else "dev"


VERSION = read_version()
