"""Inspect a PDF and return a PdfProfile used for routing. No fitz."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PdfProfile:
    page_count: int
    is_encrypted: bool
    has_text_layer: float   # 0..1, fraction of sampled pages with extractable text
    table_density: float    # mean tables-per-page across the sample
    is_scanned: bool        # derived: has_text_layer < 0.10
    max_table_cells: int = 0  # largest table seen in sample (rows*cols); 0 if none
    # F20: pikepdf.PasswordError on the ORIGINAL pdf, before sanitize. The
    # ghostscript sanitize tier transparently strips encryption and produces
    # an empty page, which makes is_encrypted (probed on the sanitized file)
    # always False — driver needs the original-pdf signal to fast-fail with
    # an actionable "encrypted: password required" reason instead of the
    # misleading "emptiness" fall-through (S4 stress).
    is_originally_encrypted: bool = False

    @property
    def est_total_tables(self) -> float:
        """Density extrapolated to the whole document."""
        return self.table_density * self.page_count


_SAMPLE_PAGES = 8


def _page_count(pdf: Path) -> int:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf), autoclose=True)
    try:
        return len(doc)
    finally:
        doc.close()


def _is_encrypted(pdf: Path) -> bool:
    try:
        import pikepdf
        with pikepdf.open(str(pdf)) as _:
            return False
    except pikepdf.PasswordError:
        return True
    except Exception:
        # treat any other pikepdf failure as "can't confirm" → assume not encrypted
        return False


def _sample_indices(n: int, k: int) -> list[int]:
    if n <= 0:
        return []
    k = min(k, n)
    if k == 1:
        return [0]
    return sorted({int(i * (n - 1) / (k - 1)) for i in range(k)})


def _text_layer_fraction(pdf: Path, page_count: int) -> float:
    """Fraction of sampled pages that yield any extractable text via pdfplumber."""
    import pdfplumber
    idx = _sample_indices(page_count, _SAMPLE_PAGES)
    if not idx:
        return 0.0
    hits = 0
    try:
        with pdfplumber.open(str(pdf)) as doc:
            for i in idx:
                try:
                    text = doc.pages[i].extract_text() or ""
                    if text.strip():
                        hits += 1
                except Exception:
                    continue
    except Exception:
        return 0.0
    return hits / len(idx)


def _table_density_pdfplumber(pdf: Path, page_count: int) -> float:
    """Mean tables per page across the sample (pdfplumber).

    Cheap but misses borderless / whitespace-aligned tables. Used as a fallback
    when the docling layout detector is unavailable.
    """
    import pdfplumber
    idx = _sample_indices(page_count, _SAMPLE_PAGES)
    if not idx:
        return 0.0
    total = 0
    counted = 0
    try:
        with pdfplumber.open(str(pdf)) as doc:
            for i in idx:
                try:
                    tables = doc.pages[i].find_tables()
                    total += len(tables)
                    counted += 1
                except Exception:
                    continue
    except Exception:
        return 0.0
    return (total / counted) if counted else 0.0


_docling_converter = None


def _get_docling_converter():
    global _docling_converter
    if _docling_converter is None:
        from docling.document_converter import DocumentConverter
        _docling_converter = DocumentConverter()
    return _docling_converter


def _table_metrics_docling(pdf: Path, page_count: int) -> tuple[float, int]:
    """Return (mean tables/page, max cells in any table) from a docling sample.

    Detects borderless / whitespace-aligned tables that pdfplumber misses, and
    captures table size so callers can route on absolute table workload, not
    just density. The converter is cached at module scope so the model load
    amortizes across PDFs in a batch. Falls back to pdfplumber-density-only on
    any failure (max_cells=0).
    """
    idx = _sample_indices(page_count, _SAMPLE_PAGES)
    if not idx:
        return 0.0, 0
    try:
        converter = _get_docling_converter()
    except Exception:
        return _table_density_pdfplumber(pdf, page_count), 0

    total = 0
    max_cells = 0
    counted = 0
    for i in idx:
        try:
            # docling page_range is 1-indexed inclusive
            result = converter.convert(str(pdf), page_range=(i + 1, i + 1))
            tables = getattr(result.document, "tables", []) or []
            total += len(tables)
            for t in tables:
                data = getattr(t, "data", None)
                if data is None:
                    continue
                cells = int(getattr(data, "num_rows", 0)) * int(getattr(data, "num_cols", 0))
                if cells > max_cells:
                    max_cells = cells
            counted += 1
        except Exception:
            continue
    if counted == 0:
        return _table_density_pdfplumber(pdf, page_count), 0
    return total / counted, max_cells


def probe(pdf: Path, original_pdf: Path | None = None) -> PdfProfile:
    """Inspect `pdf` (typically the sanitized output) and return a PdfProfile.

    `original_pdf` is the pre-sanitize source. When supplied, encryption is
    additionally probed on the original — ghostscript sanitize strips
    user-password protection silently, so probing only the sanitized file
    misses the fact that the operator never had access to the content.
    The result populates `is_originally_encrypted` (F20)."""
    n = _page_count(pdf)
    enc = _is_encrypted(pdf)
    text_frac = 0.0 if enc else _text_layer_fraction(pdf, n)
    if enc:
        density, max_cells = 0.0, 0
    else:
        density, max_cells = _table_metrics_docling(pdf, n)
    orig_enc = enc
    if original_pdf is not None and original_pdf != pdf:
        orig_enc = enc or _is_encrypted(original_pdf)
    return PdfProfile(
        page_count=n,
        is_encrypted=enc,
        has_text_layer=text_frac,
        table_density=density,
        is_scanned=(text_frac < 0.10),
        max_table_cells=max_cells,
        is_originally_encrypted=orig_enc,
    )
