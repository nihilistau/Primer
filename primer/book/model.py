"""The book as data: the reader's trail collected from the graph into chapters and stories.

Both writers (EPUB and HTML) are built from one BookPlan. Chapters are the pages with a
version, in trail order (first arrival), nested under the page they were first reached from
when that page is in the book. Chapter i (from 1, in order) is anchored `c{i}`, and its
citations link to `#c{i}-src-{n}`, using the page's stored source numbers.

Nesting: `Chapter.depth` (1..6) is the nesting the writers use for the contents. It is capped
at 6, so a chapter can sit deeper in the trail than its depth says; writers nest by depth, not
by following `parent_id`. `parent_id` is only for the "From ... in ..." link, and is set only
when the parent is in the book and comes earlier; `via_title` names the parent page either way.
"""
from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from primer.diagrams.svg import DiagramError, clean_svg

from .markdown import render, safe_url

MAX_CHAPTERS = 1000
MAX_PICTURES = 300
PICTURES_PER_CHAPTER = 6
MAX_DEPTH = 6
MAX_TITLE = 120
MAX_BY = 80
WHOLE_PAGE = "The whole page"
PICTURE_NAME = re.compile(r"[0-9a-f]{64}\.png")
MAX_SINCE = 4_102_444_800  # 1 January 2100: later (or before 1970) is not a reading date
INCLUDE_KEYS = ("pictures", "maps", "timelines", "diagrams", "stories")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
# the size estimate: base64 grows a picture by about 4/3 (plus line overhead), and each
# chapter and map plate adds roughly this much markup
BASE64_GROWTH = 1.37
CHAPTER_BYTES = 4 * 1024
MAP_BYTES = 150_000  # real plates run 18-196 KB


@dataclass
class Picture:
    media_id: str
    file: str
    caption: str


@dataclass
class MapData:
    map_id: str
    phrase: str
    places: list[dict]  # {n, name, what, lat, lon}: located places only, numbered from 1


@dataclass
class Chapter:
    node_id: str
    title: str
    via_phrase: str
    parent_id: str | None  # the in-book parent, else None
    depth: int  # 1..MAX_DEPTH
    arrived_at: float
    html: str
    sources: list[dict] = field(default_factory=list)  # {n, title, kind, url}
    pictures: list[Picture] = field(default_factory=list)
    maps: list[MapData] = field(default_factory=list)
    timeline: list[dict] | None = None
    diagram: dict | None = None  # {svg, caption}, the svg already re-cleaned
    has_video: bool = False
    via_title: str = ""  # the page it was first reached from, in the book or not


@dataclass
class Story:
    title: str
    scenes: list[dict]  # {title, text, picture: Picture | None}


@dataclass
class BookPlan:
    title: str
    by: str
    chapters: list[Chapter]
    stories: list[Story]
    first_at: float | None
    last_at: float | None
    left_out: dict = field(default_factory=lambda: {"pictures_cap": 0, "chapters_cap": 0,
                                                    "missing_media": 0})


def _scope(scope) -> dict:
    """The scope checked and normalised; ValueError when it is not one of the three kinds."""
    if not isinstance(scope, dict):
        raise ValueError("scope must be an object")
    kind = scope.get("kind")
    if kind == "all":
        return {"kind": "all"}
    if kind == "branch":
        node_id = scope.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("a branch needs a page")
        return {"kind": "branch", "node_id": node_id}
    if kind == "since":
        since = scope.get("since")
        if isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since):
            raise ValueError("since must be a time in seconds")
        if not 0 <= since <= MAX_SINCE:
            raise ValueError("since is out of range")
        return {"kind": "since", "since": float(since)}
    raise ValueError("scope kind must be all, branch or since")


def _include(include) -> dict:
    if include is None:
        include = {}
    if not isinstance(include, dict):
        raise ValueError("include must be an object")
    return {k: bool(include.get(k, True)) for k in INCLUDE_KEYS}


def default_title(graph, scope: dict) -> str:
    """"My wanderings", the branch page's title, or "Since 9 November 2026" (local date)."""
    scope = _scope(scope)
    if scope["kind"] == "branch":
        node = graph.get_node(scope["node_id"])
        if node is None:
            raise ValueError("unknown page")
        return node.title
    if scope["kind"] == "since":
        try:
            t = time.localtime(scope["since"])
        except (OSError, OverflowError, ValueError):
            raise ValueError("since is out of range") from None
        return f"Since {t.tm_mday} {_MONTHS[t.tm_mon - 1]} {t.tm_year}"
    return "My wanderings"


def _trail(graph):
    """(node rowid order, first edge into each node, versions of each node) from the graph."""
    snap = graph.snapshot()
    order = {n["id"]: i for i, n in enumerate(snap["nodes"])}
    titles = {n["id"]: n["title"] for n in snap["nodes"]}
    first: dict[str, dict] = {}
    for e in snap["edges"]:  # in rowid order, so a tie on `at` keeps the earlier edge
        if e["to_id"] in order and (e["to_id"] not in first or e["at"] < first[e["to_id"]]["at"]):
            first[e["to_id"]] = e
    versions = {nid: graph.versions(nid) for nid in order}
    return order, titles, first, versions


def _source(s) -> dict:
    kind = "web" if s.origin == "web" else (s.where or s.book or "reference")
    return {"n": s.n, "title": s.title, "kind": kind, "url": safe_url(s.url or "") or ""}


def collect(graph, scope: dict, include: dict, title: str = "", by: str = "",
            media_dir: Path | None = None, light: bool = False) -> BookPlan:
    """The book for this scope. ValueError on a bad scope or include, or when a branch's page
    is unknown or was never read.

    `light` is for counting (the preview): the same chapters in the same order, the same
    pictures (same caps and media checks), maps and stories, so `estimate` gives the same
    answer, but no Markdown is rendered and no sources, timelines or diagrams are read: each
    chapter's `html` is "", `sources` [], `timeline` and `diagram` None. Not for writing."""
    scope, include = _scope(scope), _include(include)
    order, titles, first, versions = _trail(graph)
    read = {nid for nid, vs in versions.items() if vs}
    arrived: dict[str, float] = {}
    parent: dict[str, str | None] = {}
    for nid in order:
        e = first.get(nid)
        if e is not None:
            arrived[nid] = e["at"]
            p = e["from_id"]
            parent[nid] = p if p in order and p != nid else None
        elif versions[nid]:
            arrived[nid], parent[nid] = versions[nid][0].created_at, None

    if scope["kind"] == "all":
        book = set(read)
    elif scope["kind"] == "since":
        book = {nid for nid in read if arrived[nid] >= scope["since"]}
    else:
        root = scope["node_id"]
        if root not in read:
            raise ValueError("unknown page" if root not in order else "that page was never read")
        children: dict[str, list[str]] = {}
        for nid, p in parent.items():
            if p is not None:
                children.setdefault(p, []).append(nid)
        seen, queue = {root}, deque([root])
        while queue:  # the visited set ends any cycle
            for child in children.get(queue.popleft(), ()):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        book = seen & read

    ordered = sorted(book, key=lambda nid: (arrived[nid], order[nid]))
    left_out = {"pictures_cap": 0, "chapters_cap": max(0, len(ordered) - MAX_CHAPTERS),
                "missing_media": 0}
    ordered = ordered[:MAX_CHAPTERS]
    in_book = set(ordered)

    media: dict[str, list] = {}
    for m in graph.done_media():
        if m.node_id in in_book:
            media.setdefault(m.node_id, []).append(m)
    used: set[str] = set()

    def usable(m) -> bool:
        ok = bool(m.file) and PICTURE_NAME.fullmatch(m.file) is not None
        if ok and media_dir is not None:
            ok = (Path(media_dir) / m.file).is_file()
        if not ok:
            left_out["missing_media"] += 1
        return ok

    def picture(m) -> Picture:
        used.add(m.id)
        return Picture(m.id, m.file, (m.phrase or "").strip() or WHOLE_PAGE)

    chapters: list[Chapter] = []
    depth_of: dict[str, int] = {}
    for i, nid in enumerate(ordered, start=1):
        p = parent.get(nid)
        via_title = titles.get(p, "") if p else ""
        if p in depth_of:  # in the book and placed already (cycles break here)
            depth = min(depth_of[p] + 1, MAX_DEPTH)
        else:
            p, depth = None, 1
        depth_of[nid] = depth
        e = first.get(nid)
        sources, html = [], ""
        if not light:
            latest = versions[nid][-1]
            sources = [_source(s) for s in graph.sources(nid, latest.n)]
            html = render(latest.markdown, anchor=f"c{i}", n_sources=len(sources),
                          numbers=[s["n"] for s in sources])
        ch = Chapter(nid, titles[nid], e["phrase"] if e else "", p, depth, arrived[nid], html,
                     sources, via_title=via_title)
        mine = media.get(nid, [])
        ch.has_video = any(m.kind == "video" for m in mine)
        if include["pictures"]:
            for m in mine:
                if m.kind != "image" or not usable(m):
                    continue
                if len(ch.pictures) >= PICTURES_PER_CHAPTER or len(used) >= MAX_PICTURES:
                    left_out["pictures_cap"] += 1
                    continue
                ch.pictures.append(picture(m))
        if include["maps"]:
            ch.maps = _maps(graph, nid)
        if include["timelines"] and not light:
            ch.timeline = _timeline(graph, nid)
        if include["diagrams"] and not light:
            ch.diagram = _diagram(graph, nid, i)
        chapters.append(ch)

    stories: list[Story] = []
    if include["stories"]:
        found: dict[str, dict] = {}
        for nid in ordered:
            for s in graph.stories_for(nid):
                if s["status"] == "done" and s["node_id"] in in_book:
                    found.setdefault(s["id"], s)
        for s in sorted(found.values(), key=lambda s: (s["created_at"], s["id"])):
            full = graph.get_story(s["id"]) or {"scenes": []}
            scenes = []
            for sc in full["scenes"]:
                pic = None
                m = graph.get_media(sc["media_id"]) if include["pictures"] and sc["media_id"] else None
                if m is not None and m.status == "done" and m.kind == "image":
                    if m.id in used:
                        pic = Picture(m.id, m.file, (m.phrase or "").strip() or WHOLE_PAGE)
                    elif usable(m):
                        if len(used) >= MAX_PICTURES:
                            left_out["pictures_cap"] += 1
                        else:
                            pic = picture(m)
                scenes.append({"title": sc["title"], "text": sc["text"], "picture": pic})
            stories.append(Story(s["title"], scenes))

    title = (title or "").strip()[:MAX_TITLE] or default_title(graph, scope)[:MAX_TITLE]
    times = [ch.arrived_at for ch in chapters]
    return BookPlan(title, (by or "").strip()[:MAX_BY], chapters, stories,
                    min(times) if times else None, max(times) if times else None, left_out)


def _maps(graph, node_id: str) -> list[MapData]:
    """The page's finished maps with located places, oldest first."""
    out = []
    for row in reversed(graph.maps_for(node_id)):
        if row["status"] != "done":
            continue
        full = graph.get_map(row["id"]) or {"places": []}
        located = [p for p in full["places"] if p["lat"] is not None and p["lon"] is not None]
        if located:
            out.append(MapData(row["id"], row["phrase"], [
                {"n": n, "name": p["name"], "what": p["what"], "lat": p["lat"], "lon": p["lon"]}
                for n, p in enumerate(located, start=1)]))
    return out


def _timeline(graph, node_id: str) -> list[dict] | None:
    """The events of the page's latest finished timeline that has any."""
    for row in graph.timelines_for(node_id):  # newest first
        if row["status"] == "done" and row["event_count"]:
            full = graph.get_timeline(row["id"])
            return full["events"] if full else None
    return None


def _diagram(graph, node_id: str, i: int) -> dict | None:
    """The page's latest finished diagram, cleaned again with ids prefixed `dg{i}-` (i, the
    chapter's number, is unique in the book, so ids never clash in the single HTML file);
    None when it no longer cleans."""
    for row in graph.diagrams_for(node_id):  # newest first
        if row["status"] != "done":
            continue
        full = graph.get_diagram(row["id"])
        if full is None:
            return None
        try:
            svg, _labels = clean_svg(full["svg"], prefix=f"dg{i}-")
        except (DiagramError, ValueError):
            return None
        return {"svg": svg, "caption": full["caption"]}
    return None


def estimate(plan: BookPlan, media_dir: Path) -> dict:
    """{chapters, pictures, bytes}: picture sizes grown for base64, plus markup per chapter and
    per map plate. `pictures` counts each picture once, but the bytes count every copy the
    single HTML file embeds: the cover repeats the book's first picture, and a scene picture
    already shown in a chapter is embedded again."""
    uses: list[tuple[str, str]] = [(p.media_id, p.file) for ch in plan.chapters for p in ch.pictures]
    uses += [(sc["picture"].media_id, sc["picture"].file) for s in plan.stories for sc in s.scenes
             if sc["picture"] is not None]
    if uses:
        uses.append(uses[0])  # the cover
    sizes: dict[str, int] = {}
    for _mid, name in uses:
        if name not in sizes:
            sizes[name] = 0
            if PICTURE_NAME.fullmatch(name or ""):
                try:
                    sizes[name] = (Path(media_dir) / name).stat().st_size
                except OSError:
                    pass
    size = sum(sizes[name] for _mid, name in uses)
    maps = sum(len(ch.maps) for ch in plan.chapters)
    return {"chapters": len(plan.chapters), "pictures": len({mid for mid, _name in uses}),
            "bytes": int(size * BASE64_GROWTH) + CHAPTER_BYTES * len(plan.chapters) + MAP_BYTES * maps}
