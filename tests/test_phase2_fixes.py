"""Tests for Phase-2/3 robustness/UX fixes (2026-05-01).

See docs/superpowers/specs/2026-04-30-pipeline-eval-findings.md for
the findings each test covers (F18-F31)."""
from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from alchemd import cli, driver, preflight


# --- F20: encrypted-PDF detection --------------------------------------------


def _make_encrypted_pdf(dst: Path, password: str = "secret") -> Path:
    """Create a 1-page encrypted PDF derived from clean.pdf, written to dst.

    Uses pypdf so the test is self-contained — no dependency on a fixture
    file living under Testing/."""
    from pypdf import PdfReader, PdfWriter

    src_clean = Path(__file__).resolve().parents[1] / "Testing" / "clean.pdf"
    if not src_clean.exists():
        pytest.skip(f"clean.pdf fixture not present: {src_clean}")

    reader = PdfReader(str(src_clean))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    writer.encrypt(user_password=password, owner_password=None, use_128bit=True)
    with open(dst, "wb") as f:
        writer.write(f)
    return dst


def test_preflight_probe_detects_originally_encrypted(clean_pdf, tmp_path):
    """preflight.probe(work_path, original_pdf=...) must report
    is_originally_encrypted=True when the ORIGINAL pdf is password-protected,
    even if work_path (sanitized) appears unlocked. Models the real driver
    flow: sanitize already stripped encryption, so work_path=sanitized opens
    fine, but original_pdf is the encrypted source."""
    enc = _make_encrypted_pdf(tmp_path / "encrypted.pdf")
    # Use clean_pdf as the stand-in for "sanitized output" (sanitize would
    # have produced a 1-3p empty pdf in reality; clean_pdf is fine for the
    # _page_count call that probe() makes).
    profile = preflight.probe(clean_pdf, original_pdf=enc)
    assert profile.is_originally_encrypted is True


def test_preflight_probe_unencrypted_original(clean_pdf):
    """is_originally_encrypted is False on plain pdfs."""
    profile = preflight.probe(clean_pdf, original_pdf=clean_pdf)
    assert profile.is_originally_encrypted is False


def test_driver_short_circuits_on_encrypted_original(tmp_path):
    """driver.process_one must short-circuit with stage='encrypted' when the
    original pdf is encrypted and no --password was supplied. The failure
    reason must reference 'encrypted' (not the misleading 'emptiness' that
    S4 originally produced)."""
    enc = _make_encrypted_pdf(tmp_path / "doc.pdf")
    out = tmp_path / "out"
    out.mkdir()
    result = driver.process_one(enc, out, engines={}, skip_sanitize=False)
    assert result.ok is False
    assert "encrypted" in result.reason.lower()
    assert "password" in result.reason.lower()


# --- F23: catch OSError on _atomic_write -------------------------------------


def test_atomic_write_disk_full_returns_clean_failure(tmp_path):
    """_atomic_write must propagate OSError, but the caller in driver._try_engines
    must catch it and return a clean PdfResult(ok=False) rather than letting
    the traceback escape to the cli."""
    from alchemd import driver as drv

    real_atomic = drv._atomic_write
    state = {"tripped": False}

    def faulty(path: Path, content: str) -> None:
        if not state["tripped"] and path.suffix == ".md":
            state["tripped"] = True
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_atomic(path, content)

    # Smoke test: the exception must be classified as a disk error and not
    # bubble out as a generic traceback. We construct a minimal driver call
    # that reaches _atomic_write via a stubbed engine.
    from alchemd.engines.base import EngineResult

    class _StubEngine:
        name = "stub"

        def convert(self, pdf, out_dir, page_count=None):
            # Long enough to clear the quality emptiness threshold
            # (150 chars/page × clean.pdf's 3 pages).
            return EngineResult(
                engine="stub", markdown="paragraph text " * 500, images=[],
                elapsed=0.1, notes=[])

    pdf = Path(__file__).resolve().parents[1] / "Testing" / "clean.pdf"
    if not pdf.exists():
        pytest.skip("clean.pdf fixture not present")
    out = tmp_path / "out"
    out.mkdir()

    with patch.object(drv, "_atomic_write", faulty):
        result = drv.process_one(pdf, out, engines={"marker": _StubEngine(),
                                                    "docling": _StubEngine()},
                                 skip_sanitize=True)
    assert result.ok is False
    assert "disk" in result.reason.lower() or "no space" in result.reason.lower() \
        or "write" in result.reason.lower()


def test_cli_exit_disk_error_constant_exists():
    """F23: a distinct exit code for batch-level disk failures."""
    assert hasattr(cli, "EXIT_DISK_ERROR")
    assert cli.EXIT_DISK_ERROR == 5
    # Must be distinct from existing codes.
    codes = {cli.EXIT_OK, cli.EXIT_USAGE, cli.EXIT_PER_PDF_FAILURES,
             cli.EXIT_CUDA_ABORTED, cli.EXIT_LOCKED, cli.EXIT_DISK_ERROR}
    assert len(codes) == 6


# --- F24: orphan .tmp cleanup on _atomic_write failure -----------------------


def test_atomic_write_cleans_up_tmp_on_failure(tmp_path):
    """When tmp.write_text or os.replace fails, the orphan .md.tmp must be
    unlinked. Re-running the conversion shouldn't leave leftover .tmp files
    (cosmetic but accumulates over time on flaky storage)."""
    from alchemd import driver as drv

    target = tmp_path / "doc.md"
    tmp = target.with_suffix(".md.tmp")

    real_replace = os.replace
    def faulty_replace(src, dst):
        raise OSError(errno.ENOSPC, "fail at replace")

    with patch("os.replace", faulty_replace):
        with pytest.raises(OSError):
            drv._atomic_write(target, "content")

    assert not tmp.exists(), f"orphan .tmp survived: {tmp}"
    assert not target.exists(), "target .md should not exist either"


# --- F18: children skip parent-only writes -----------------------------------


def test_run_batch_skips_chunks_and_runlog_when_child_env_set(monkeypatch, tmp_path):
    """When _CHILD_HOLDS_LOCK_ENV=1 (i.e. parent is running this child to
    handle a single PDF), _run_batch must NOT write chunks.json or append to
    run.log — those are batch-level state owned by the parent."""
    output = tmp_path / "out"
    output.mkdir()
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.setenv(cli._CHILD_HOLDS_LOCK_ENV, "1")
    monkeypatch.setattr("sys.argv",
                        ["alchemd",
                         "--input-dir", str(input_dir),
                         "--output-dir", str(output),
                         "-y"])
    rc = cli.main()
    # Empty input dir → EXIT_OK. Critically, no chunks.json / run.log written.
    assert rc == cli.EXIT_OK
    assert not (output / "chunks.json").exists()
    assert not (output / "run.log").exists()


def test_run_batch_writes_chunks_and_runlog_when_no_child_env(monkeypatch, tmp_path):
    """Sanity check the inverse: when env var is NOT set AND there is at
    least one PDF, _run_batch DOES write run.log."""
    output = tmp_path / "out"
    output.mkdir()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    # A valid 1p PDF (header is enough — we stub the conversion).
    (input_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    # Stub the per-PDF subprocess to "succeed" without doing any work.
    monkeypatch.setattr(cli, "_process_one_subprocess",
                        lambda *a, **kw: (False, "", 0.0, 0.1))
    monkeypatch.setattr(cli, "_gpu_health_probe", lambda: None)
    monkeypatch.setattr(cli, "find_existing_output", lambda *a, **kw: None)
    monkeypatch.setattr(cli.slicing, "page_count", lambda p: 1)

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.delenv(cli._CHILD_HOLDS_LOCK_ENV, raising=False)
    monkeypatch.setattr("sys.argv",
                        ["alchemd",
                         "--input-dir", str(input_dir),
                         "--output-dir", str(output),
                         "-y"])
    cli.main()
    # run.log should exist for a non-child invocation that had ≥1 PDF.
    assert (output / "run.log").exists()


# --- F19: timestamp prefix stripped from FAIL reason -------------------------


def test_read_last_failure_reason_strips_iso8601_prefix(tmp_path):
    """_read_last_failure_reason must strip a leading [YYYY-MM-DDTHH:MM:SSZ]
    prefix so the user-facing FAIL line doesn't leak debug.log timestamps."""
    debug = tmp_path / "debug.log"
    debug.write_text(
        "=== sanitize ===\n"
        "[2026-04-30T19:23:31Z] tier=ghostscript ok=True\n"
        "=== done ===\n"
        "[2026-04-30T19:23:31Z] status=failed reason=quality failed: emptiness\n",
        encoding="utf-8",
    )
    reason = cli._read_last_failure_reason(debug)
    # The reason must NOT start with a timestamp prefix or the redundant
    # 'status=failed reason=' wrapper that the debug.log convention adds.
    assert not reason.startswith("["), f"reason still starts with [: {reason!r}"
    assert not reason.startswith("status=failed"), (
        f"reason still has status=failed prefix: {reason!r}")
    assert "quality failed" in reason


# --- F21: CPU-aware adaptive timeout -----------------------------------------


def test_adaptive_timeout_grows_on_cpu(monkeypatch):
    """When torch.cuda.is_available() returns False, the adaptive timeout
    must grow proportionally so marker doesn't false-timeout on CPU. With
    a 6x multiplier, a 1p PDF goes from 90s → 540s (still under 4h cap)."""
    from alchemd.engines import subprocess_engine

    # Force the cached cuda-availability flag to "no GPU".
    monkeypatch.setattr(subprocess_engine, "_cuda_available_cached", False)
    timeout_cpu = subprocess_engine.adaptive_timeout(page_count=1, cap=14400)

    monkeypatch.setattr(subprocess_engine, "_cuda_available_cached", True)
    timeout_gpu = subprocess_engine.adaptive_timeout(page_count=1, cap=14400)

    assert timeout_cpu > timeout_gpu, (
        f"CPU timeout should exceed GPU; got CPU={timeout_cpu}, GPU={timeout_gpu}")
    # 6x default for a 1p doc.
    assert timeout_cpu == timeout_gpu * 6 or timeout_cpu >= timeout_gpu * 5


# --- F31: sliced-PDF failure reason wired up ---------------------------------


def test_process_sliced_propagates_failed_slice_reason(monkeypatch, tmp_path):
    """When _process_sliced has any_slice_failed, the failed slice's
    debug.log reason must be visible to cli's run.log entry — not the bare
    'unknown (no debug.log written)' that S6 abort showed."""
    from alchemd import slicing

    src = tmp_path / "book.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    output = tmp_path / "out"
    output.mkdir()

    fake_slice_paths = [tmp_path / f"book_part_{i:02d}.pdf" for i in range(1, 4)]
    for sp in fake_slice_paths:
        sp.write_bytes(b"%PDF-1.4\n%%EOF\n")

    monkeypatch.setattr(slicing, "slice_pdf", lambda pdf, out_dir, chunk: list(fake_slice_paths))

    # Stub _process_one_subprocess to fail on slice 02 with a known reason.
    def stub_subprocess(pdf_path, output_dir, args, input_dir_override=None):
        # Write a debug.log that simulates a failed slice run.
        stem_dir = output_dir / pdf_path.stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        if "part_02" in pdf_path.name:
            (stem_dir / "debug.log").write_text(
                "=== done ===\n"
                "[2026-05-01T08:00:00Z] status=failed reason=marker: synthetic\n",
                encoding="utf-8",
            )
            return (False, "", 0.0, 0.0)
        # Slice 01 succeeds; slice 03 never reached.
        (stem_dir / f"{pdf_path.stem}.md").write_text("ok", encoding="utf-8")
        (stem_dir / "manifest.json").write_text('{"engine":"marker","quality_score":1.0}',
                                                 encoding="utf-8")
        return (True, "marker", 1.0, 0.0)

    monkeypatch.setattr(cli, "_process_one_subprocess", stub_subprocess)

    class _Args:
        engine = "auto"
        no_sanitize = True
        keep_temp = True
        force = False
        input_dir = tmp_path

    args = _Args()
    ok, engine_used, score, elapsed = cli._process_sliced(src, output, args, page_count=450)
    assert ok is False
    # The merged-stem debug.log must now exist, surfacing the failed slice's reason.
    merged_debug = output / src.stem / "debug.log"
    assert merged_debug.exists()
    text = merged_debug.read_text(encoding="utf-8")
    assert "part_02" in text or "slice 2" in text or "slice 02" in text.lower()
    assert "marker: synthetic" in text


# --- F30: children skip env.check -------------------------------------------


def test_env_check_skipped_in_child_subprocess(monkeypatch, tmp_path):
    """When _CHILD_HOLDS_LOCK_ENV=1, env.check must not run (parent already
    validated). Saves 30-50s × N PDFs in subprocess mode."""
    output = tmp_path / "out"
    output.mkdir()
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    calls = {"env_check": 0}

    def counting_env_check(venv, auto_yes):
        calls["env_check"] += 1

    monkeypatch.setattr(cli.env, "check", counting_env_check)
    monkeypatch.setenv(cli._CHILD_HOLDS_LOCK_ENV, "1")
    monkeypatch.setattr("sys.argv",
                        ["alchemd",
                         "--input-dir", str(input_dir),
                         "--output-dir", str(output),
                         "-y"])
    rc = cli.main()
    assert rc == cli.EXIT_OK
    assert calls["env_check"] == 0, (
        f"env.check should be skipped in child mode; ran {calls['env_check']} times")
