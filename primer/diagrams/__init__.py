"""The diagram lens: the writing model draws how something works as SVG, rebuilt from an allowlist."""
from __future__ import annotations


class DiagramError(Exception):
    """A drawing that can't be used; the message is the reason the reader sees."""
