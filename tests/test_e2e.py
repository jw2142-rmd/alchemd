import shutil
from pathlib import Path

import pypdf


TESTING = Path(__file__).resolve().parents[1] / "Testing"

# Cap fast-suite e2e runtime: ignore any PDF in Testing/ over this many
# pages. Without a cap, this test silently grows linearly with whatever
# the user happens to have staged for ad-hoc engine work — the 2026-04-30
# fast-suite hang traced to a 1667p neurology book + 449p wall_street
# both being in Testing/, pushing this test well past an hour.
_MAX_FAST_SUITE_PAGES = 50


def _small_pdfs() -> list[Path]:
    """Subset of Testing/*.pdf that's small enough for fast-suite use."""
    out: list[Path] = []
    for pdf in sorted(TESTING.glob("*.pdf")):
        try:
            pages = len(pypdf.PdfReader(str(pdf)).pages)
        except Exception:
            continue
        if pages <= _MAX_FAST_SUITE_PAGES:
            out.append(pdf)
    return out


def test_e2e_batch_runs_and_emits_debug_logs(monkeypatch, tmp_path):
    """Smoke-test the full batch flow on the small PDFs in Testing/.

    Bounded runtime: only PDFs with <= _MAX_FAST_SUITE_PAGES are included,
    and the input directory is a per-test staging copy so growth in
    Testing/ never silently slows the suite further.
    """
    from alchemd.cli import main

    candidates = _small_pdfs()
    if not candidates:
        import pytest
        pytest.skip(
            f"no Testing/*.pdf with <= {_MAX_FAST_SUITE_PAGES} pages")

    staging = tmp_path / "input"
    staging.mkdir()
    for pdf in candidates:
        shutil.copy2(pdf, staging / pdf.name)

    out = tmp_path / "output"
    monkeypatch.setattr("sys.argv",
                        ["alchemd", "--input-dir", str(staging),
                         "--output-dir", str(out), "-y"])
    rc = main()

    for pdf in candidates:
        assert (out / pdf.stem / "debug.log").exists(), (
            f"missing debug.log for {pdf.name}")
    assert (out / "run.log").exists()
    assert rc in (0, 2)
