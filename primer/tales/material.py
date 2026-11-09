"""Choosing the pages a next chapter draws on: those the reader opened since the last chapter
(for the first chapter, since a day before the tale began: the reading that led to it counts),
not used by an earlier chapter, ranked by time spent, opens, recency."""
from __future__ import annotations

import re

LIMIT = 3
EXCERPT_CHARS = 2500
LOOK_BACK_S = 24 * 3600  # chapter 1 also draws on pages read this long before the tale began
_TITLE_LINE = re.compile(r"\A\s*#[^\n]*\n?")


def _since(graph, tale_id: str) -> float:
    chapters = graph.tale_chapters(tale_id)
    if chapters:
        return float(chapters[-1]["created_at"])
    tale = graph.get_tale(tale_id)
    return max(0.0, float(tale["created_at"]) - LOOK_BACK_S) if tale else 0.0


def candidates(graph, tale_id: str) -> list[dict]:
    """Every unused page read since the last chapter, best first: [{node_id, title, opens,
    seconds, last}]."""
    used = graph.used_pages(tale_id)
    pages = [p for p in graph.pages_read_since(_since(graph, tale_id)) if p["node_id"] not in used]
    pages.sort(key=lambda p: (-p["seconds"], -p["opens"], -p["last"], p["node_id"]))
    return pages


def waiting_count(graph, tale_id: str) -> int:
    """How many unused pages were read since the last chapter (not capped)."""
    return len(candidates(graph, tale_id))


def excerpt(markdown: str, limit: int = EXCERPT_CHARS) -> str:
    """The page without its leading '# title' line, cut at a paragraph or word boundary to at
    most `limit` characters."""
    text = _TITLE_LINE.sub("", markdown or "", count=1).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    para = cut.rfind("\n\n")
    if para >= limit // 2:
        return cut[:para].rstrip()
    space = max(cut.rfind(" "), cut.rfind("\n"))
    return (cut[:space] if space > 0 else text[:limit]).rstrip()


def pick_material(graph, tale_id: str, *, chosen: list[str] | None = None, limit: int = LIMIT,
                  excerpt_chars: int = EXCERPT_CHARS) -> list[dict]:
    """Up to `limit` pages as [{node_id, title, excerpt, seconds, opens}]. `chosen` node ids (the
    reader's picks) are used as given, in order, skipping unknown pages and pages with no stored
    version; otherwise the best of the unused pages read since the last chapter. [] when there
    is nothing to draw on."""
    if chosen is not None:
        stats = {p["node_id"]: p for p in graph.pages_read_since(0.0)}
        pages, seen = [], set()
        for node_id in chosen:
            if not isinstance(node_id, str) or node_id in seen:
                continue
            seen.add(node_id)
            node = graph.get_node(node_id)
            if node is not None and graph.latest_version(node_id) is not None:
                s = stats.get(node_id, {})
                pages.append({"node_id": node_id, "title": node.title,
                              "opens": s.get("opens", 0), "seconds": s.get("seconds", 0.0)})
    else:
        pages = candidates(graph, tale_id)
    out = []
    for p in pages[:max(0, limit)]:
        version = graph.latest_version(p["node_id"])
        out.append({"node_id": p["node_id"], "title": p["title"],
                    "excerpt": excerpt(version.markdown, excerpt_chars),
                    "seconds": p["seconds"], "opens": p["opens"]})
    return out
