"""Story prompt and a tolerant parser of the scenes the model writes."""
from __future__ import annotations

import re

MAX_SCENES = 5

_SYSTEM = (
    "You are the Illustrated Primer, a storyteller. Retell the page you are given as a short story "
    "told aloud to a curious child: 3 to 5 scenes, each opening with a `## Title` line and followed "
    "by 60 to 120 words of plain prose, about 400 words in all. Speak directly and warmly, keep "
    "every fact from the page true, and make each scene something one picture could show. "
    "Use no lists, no bold, no links and no citations. Never mention being an AI. "
    "Output only the scenes."
)

_HEADING = re.compile(r"^[ \t]{0,3}(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)


def story_messages(title: str, markdown: str) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Page title: {title}\n\n{markdown}"},
    ]


def parse_scenes(markdown: str, title: str) -> list[dict]:
    """Scenes (`{"title", "text"}`) from `## ` headings: any preamble and empty scenes are dropped,
    a story with no usable heading is one scene named `title`, and scenes beyond five are merged
    into the fifth. Empty input gives no scenes."""
    markdown = (markdown or "").strip()
    if not markdown:
        return []
    heads = list(_HEADING.finditer(markdown))
    if any(len(m.group(1)) > 1 for m in heads):  # a lone `# Title` above `##` scenes is no scene
        heads = [m for m in heads if len(m.group(1)) > 1]
    scenes: list[dict] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(markdown)
        body = markdown[m.end():end].strip()
        if body:
            scenes.append({"title": m.group(2).strip().strip("*_ ") or title, "text": body})
    if not scenes:
        body = _HEADING.sub(lambda m: m.group(2), markdown).strip()
        return [{"title": title, "text": body}] if body else []
    if len(scenes) > MAX_SCENES:
        extra = scenes[MAX_SCENES:]
        scenes = scenes[:MAX_SCENES]
        scenes[-1] = {"title": scenes[-1]["title"],
                      "text": "\n\n".join([scenes[-1]["text"]] + [s["text"] for s in extra])}
    return scenes
