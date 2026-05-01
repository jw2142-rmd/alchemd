"""Quality gatekeeper. Returns a report; the driver decides what to do with it."""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path

from alchemd.preflight import PdfProfile

_EMPTINESS_CHARS_PER_PAGE = 150
_GARBLED_MAX_FRACTION = 0.35
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s")
_TABLE_RE = re.compile(r"\n\|[^\n]*\|\n\|[\s:-]+\|")
_PRINTABLE = set(string.printable)


@dataclass
class QualityReport:
    passed: bool
    score: float  # 0..1
    issues: list[str] = field(default_factory=list)


def _garbled_fraction(text: str) -> float:
    if not text:
        return 1.0
    bad = sum(1 for c in text if (c not in _PRINTABLE) and (not c.isalpha()))
    return bad / len(text)


def check(markdown: str, profile: PdfProfile, images: list[Path],
          images_dir: Path) -> QualityReport:
    issues: list[str] = []
    score = 1.0

    chars_per_page = len(markdown) / max(1, profile.page_count)
    if chars_per_page < _EMPTINESS_CHARS_PER_PAGE:
        issues.append(f"emptiness: {chars_per_page:.0f} chars/page < {_EMPTINESS_CHARS_PER_PAGE}")
        score -= 0.5

    garbled = _garbled_fraction(markdown)
    if garbled > _GARBLED_MAX_FRACTION:
        issues.append(f"garbled ratio {garbled:.2f} > {_GARBLED_MAX_FRACTION}")
        score -= 0.3

    headings = len(_HEADING_RE.findall(markdown))
    if profile.page_count >= 50 and headings == 0:
        issues.append(f"heading sanity: zero headings in {profile.page_count}-page doc")
        score -= 0.1

    for img in images:
        if not img.exists():
            issues.append(f"image missing: {img.name}")
            score -= 0.3
            break

    if profile.table_density >= 1.5 and not _TABLE_RE.search("\n" + markdown):
        issues.append(f"table sanity: profile reported density {profile.table_density:.1f} "
                      f"but no markdown tables found")
        score -= 0.1

    hard_fail_markers = ("emptiness", "garbled ratio", "image missing")
    passed = all(not any(m in i for m in hard_fail_markers) for i in issues)

    return QualityReport(passed=passed, score=max(0.0, score), issues=issues)
