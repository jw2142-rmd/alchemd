"""Slice large PDFs into fixed-page parts and merge per-part outputs.

Marker has historically been the single-point-of-failure path for very large
books — a 449p run only succeeded after the CHUNK_WORKERS=1 heap-corruption
fix, and a 1667p neurology textbook needed manual slicing to avoid silent
mid-book corruption. This module exposes the same slice/process/merge flow
as a pair of library functions so cli.py can invoke it automatically for
large PDFs (without forcing the user to remember the 3-step shell pipeline).

Public surface:
    page_count(pdf)            — cheap pypdfium2 page count
    slice_pdf(pdf, out_dir, chunk_size) -> list[Path]  — chunked sub-PDFs
    merge_parts(stem, parts_dir, out_dir) -> Path      — merged <stem>.md

scripts/slice_pdf.py and scripts/merge_marker_parts.py are thin wrappers
around these functions so the standalone CLI surface stays identical.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pypdfium2 as pp


_IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def page_count(pdf: Path) -> int:
    """Cheap page-count probe; pypdfium2 only, no model loads."""
    src = pp.PdfDocument(str(pdf))
    try:
        return len(src)
    finally:
        src.close()


def slice_pdf(pdf: Path, out_dir: Path, chunk_size: int = 150) -> list[Path]:
    """Split <pdf> into <stem>_part_NN.pdf files of <chunk_size> pages each.

    Returns the list of slice paths in part order. Last slice gets the
    remainder. Caller is responsible for the out_dir lifecycle (creation
    and cleanup) — slice_pdf does not delete prior contents.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf.stem
    src = pp.PdfDocument(str(pdf))
    try:
        n = len(src)
        parts: list[Path] = []
        for i in range(0, n, chunk_size):
            end = min(i + chunk_size, n)
            part_idx = i // chunk_size + 1
            dst = pp.PdfDocument.new()
            dst.import_pages(src, list(range(i, end)))
            out_path = out_dir / f"{stem}_part_{part_idx:02d}.pdf"
            dst.save(out_path)
            parts.append(out_path)
        return parts
    finally:
        src.close()


def merge_parts(stem: str, parts_dir: Path, out_dir: Path) -> Path:
    """Concatenate <parts_dir>/<stem>_part_NN/<stem>_part_NN.md into a single
    <out_dir>/<stem>/<stem>.md, copying images with a partNN_ prefix to avoid
    cross-part filename collisions, and writing an aggregate manifest.json.

    Raises FileNotFoundError if no part dirs exist or if a part is missing
    its .md output (a partial merge would silently lose pages).
    """
    part_dirs = sorted(p for p in parts_dir.glob(f"{stem}_part_*") if p.is_dir())
    if not part_dirs:
        raise FileNotFoundError(
            f"no parts found at {parts_dir}/{stem}_part_*")

    merged_dir = out_dir / stem
    merged_imgs = merged_dir / "images"
    merged_imgs.mkdir(parents=True, exist_ok=True)

    md_chunks: list[str] = []
    manifest_parts: list[dict] = []
    total_elapsed = 0.0
    all_issues: list[dict] = []

    for part_dir in part_dirs:
        part_stem = part_dir.name
        m = re.search(r"_part_(\d+)$", part_stem)
        if not m:
            continue
        i = int(m.group(1))
        prefix = f"part{i:02d}_"

        md_path = part_dir / f"{part_stem}.md"
        if not md_path.exists():
            raise FileNotFoundError(f"missing part output: {md_path}")
        md = md_path.read_text(encoding="utf-8")

        def _rewrite(match: re.Match) -> str:
            alt, target = match.group(1), match.group(2)
            if target.startswith(("http://", "https://", "data:")):
                return match.group(0)
            bare = target.split("/")[-1]
            return f"![{alt}](./images/{prefix}{bare})"

        md = _IMG_REF_RE.sub(_rewrite, md)

        imgs_dir = part_dir / "images"
        if imgs_dir.is_dir():
            for img in imgs_dir.iterdir():
                if img.is_file():
                    shutil.copy2(img, merged_imgs / f"{prefix}{img.name}")

        md_chunks.append(f"<!-- ===== {part_stem} ===== -->\n\n{md}\n")

        mf_path = part_dir / "manifest.json"
        if mf_path.exists():
            try:
                mf = json.loads(mf_path.read_text(encoding="utf-8"))
                manifest_parts.append({"part": i, **mf})
                total_elapsed += float(mf.get("elapsed_sec", 0) or 0)
                for q in mf.get("quality_issues") or []:
                    all_issues.append({"part": i, "issue": q})
            except Exception as e:
                manifest_parts.append({"part": i, "manifest_error": str(e)})

    final_md = merged_dir / f"{stem}.md"
    # Atomic write so a crash mid-merge can't leave a half-written .md that
    # find_existing_output would treat as a cache hit (Phase-1 finding F4).
    _md_tmp = final_md.with_suffix(".md.tmp")
    _md_tmp.write_text("\n".join(md_chunks), encoding="utf-8")
    os.replace(_md_tmp, final_md)

    engines_used = sorted({p.get("engine") for p in manifest_parts
                           if p.get("engine")})
    final_manifest = {
        "stem": stem,
        "merged_from_parts": len(part_dirs),
        # Auto-sliced runs may use different engines per slice (the router
        # decides per-slice). Record the set so callers don't have to scan
        # the per-part manifests.
        "engines_used": engines_used or ["unknown"],
        "elapsed_sec_total": total_elapsed,
        "quality_issues": all_issues,
        "parts": manifest_parts,
    }
    _mf_path = merged_dir / "manifest.json"
    _mf_tmp = _mf_path.with_suffix(".json.tmp")
    _mf_tmp.write_text(json.dumps(final_manifest, indent=2), encoding="utf-8")
    os.replace(_mf_tmp, _mf_path)
    return final_md
