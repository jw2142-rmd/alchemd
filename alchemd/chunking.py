"""Split markdown on H1/H2/H3 boundaries. H4+ stays inside its parent section."""
from __future__ import annotations

import re


_SPLIT_RE = re.compile(r"\n(?=#{1,3}(?!#) )")
_HEADING_RE = re.compile(r"^#{1,3}(?!#) (.+)")


def chunk(markdown: str, source: str, engine: str,
          quality_score: float) -> list[dict]:
    sections = _SPLIT_RE.split(markdown)
    out: list[dict] = []
    for i, section in enumerate(sections):
        section = section.strip()
        if not section:
            continue
        m = _HEADING_RE.match(section)
        heading = m.group(1).strip() if m else "Intro"
        out.append({
            "source": source,
            "section_index": i,
            "heading": heading,
            "text": section,
            "engine": engine,
            "quality_score": quality_score,
        })
    return out
