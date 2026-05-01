"""Engine-agnostic markdown cleanup. Pure function — no I/O."""
from __future__ import annotations

import re
from pathlib import Path

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\n(\w)")
_TRIPLE_BLANK_RE = re.compile(r"\n{3,}")


def _rewrite_image(md: str, images_dir: Path) -> str:
    images_dir_str = str(images_dir).replace("\\", "/")

    def _sub(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        src_norm = src.replace("\\", "/")
        if src_norm.startswith(("http://", "https://", "data:")):
            return m.group(0)
        if src_norm.startswith(images_dir_str):
            return f"![{alt}](./images/{Path(src).name})"
        if "/" not in src_norm:
            return f"![{alt}](./images/{src_norm})"
        return m.group(0)

    return _IMG_RE.sub(_sub, md)


def _clean_tables(md: str, notes: list[str]) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"\|[\s:-]+\|", lines[i + 1]):
            header = line
            sep = lines[i + 1]
            header_cols = header.count("|") - 1
            out.append(header)
            out.append(sep)
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                row = lines[i]
                row_cols = row.count("|") - 1
                if row_cols == header_cols:
                    out.append(row)
                else:
                    notes.append(f"dropped malformed table row (cols={row_cols}, expected={header_cols}): {row[:60]}")
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def clean(markdown: str, images_dir: Path, notes: list[str]) -> str:
    md = _rewrite_image(markdown, images_dir)
    md = _HYPHEN_BREAK_RE.sub(r"\1\2", md)
    md = _clean_tables(md, notes)
    md = _TRIPLE_BLANK_RE.sub("\n\n", md)
    return md.strip() + "\n"
