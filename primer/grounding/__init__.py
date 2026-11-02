"""Grounding: pages written from an offline Wikipedia (kiwix-serve) or the web."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MODES = ("storyteller", "reference", "web")


@dataclass(frozen=True)
class Source:
    n: int
    title: str
    origin: Literal["reference", "web"]
    text: str
    book: str = ""
    path: str = ""
    url: str = ""
    where: str = ""  # the book title (reference) or the web domain
