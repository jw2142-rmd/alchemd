"""Tests for alchemd.slicing — page count, slice, merge."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from alchemd import slicing


def _make_pdf(path: Path, n_pages: int) -> None:
    """Generate a minimal n-page PDF with reportlab. One numbered page each."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i in range(1, n_pages + 1):
        c.drawString(72, 720, f"page {i}")
        c.showPage()
    c.save()


@pytest.fixture
def tiny_pdf(tmp_path) -> Path:
    p = tmp_path / "tiny.pdf"
    _make_pdf(p, 30)
    return p


def test_page_count(tiny_pdf):
    assert slicing.page_count(tiny_pdf) == 30


def test_slice_pdf_exact_chunks(tiny_pdf, tmp_path):
    out_dir = tmp_path / "slices"
    parts = slicing.slice_pdf(tiny_pdf, out_dir, chunk_size=10)
    assert len(parts) == 3
    assert all(p.exists() for p in parts)
    assert [p.name for p in parts] == [
        "tiny_part_01.pdf", "tiny_part_02.pdf", "tiny_part_03.pdf",
    ]
    assert all(slicing.page_count(p) == 10 for p in parts)


def test_slice_pdf_remainder_in_last_part(tmp_path):
    pdf = tmp_path / "src.pdf"
    _make_pdf(pdf, 25)
    parts = slicing.slice_pdf(pdf, tmp_path / "slices", chunk_size=10)
    assert len(parts) == 3
    assert slicing.page_count(parts[0]) == 10
    assert slicing.page_count(parts[1]) == 10
    # Remainder lives in the last part — losing pages here would silently
    # truncate a book, so this is a load-bearing assertion.
    assert slicing.page_count(parts[2]) == 5


def test_merge_parts_combines_md_and_prefixes_images(tmp_path):
    parts_dir = tmp_path / "parts"
    out_dir = tmp_path / "out"
    stem = "book"
    # Build two fake per-part outputs that imitate the driver layout.
    for i, body in enumerate(["alpha\n\n![](fig.png)", "beta"], start=1):
        part = parts_dir / f"{stem}_part_{i:02d}"
        (part / "images").mkdir(parents=True)
        (part / f"{stem}_part_{i:02d}.md").write_text(body, encoding="utf-8")
        (part / "images" / "fig.png").write_text("img-bytes", encoding="utf-8")
        (part / "manifest.json").write_text(json.dumps({
            "engine": "marker" if i == 1 else "docling",
            "elapsed_sec": 1.0 * i,
        }), encoding="utf-8")

    final_md = slicing.merge_parts(stem, parts_dir, out_dir)

    md_text = final_md.read_text(encoding="utf-8")
    # Image refs in part 1 must be rewritten with the part01_ prefix so
    # they don't collide with part02_'s identically-named fig.png.
    assert "./images/part01_fig.png" in md_text
    assert "alpha" in md_text and "beta" in md_text
    # Images copied with prefixes to the merged dir (collision-safe).
    merged_imgs = sorted((out_dir / stem / "images").iterdir())
    assert [p.name for p in merged_imgs] == ["part01_fig.png", "part02_fig.png"]
    # Manifest records every engine the per-slice router landed on.
    manifest = json.loads((out_dir / stem / "manifest.json").read_text())
    assert manifest["merged_from_parts"] == 2
    assert sorted(manifest["engines_used"]) == ["docling", "marker"]
    assert manifest["elapsed_sec_total"] == pytest.approx(3.0)


def test_merge_parts_raises_on_no_parts(tmp_path):
    with pytest.raises(FileNotFoundError, match="no parts found"):
        slicing.merge_parts("ghost", tmp_path, tmp_path)


def test_merge_parts_raises_on_missing_md(tmp_path):
    # A part dir with no .md inside would silently drop a chunk of the
    # book; merge_parts should fail loudly instead.
    part = tmp_path / "book_part_01"
    part.mkdir()
    with pytest.raises(FileNotFoundError, match="missing part output"):
        slicing.merge_parts("book", tmp_path, tmp_path / "out")
