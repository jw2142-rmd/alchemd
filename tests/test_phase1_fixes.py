"""Tests for Phase-1 robustness/UX fixes (2026-04-30).

See docs/superpowers/specs/2026-04-30-pipeline-eval-findings.md for the
findings each test covers (F1-F16)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from alchemd import cli, driver, router, slicing
from alchemd.preflight import PdfProfile


# --- F1: output-dir lockfile -------------------------------------------------


def test_output_dir_lock_blocks_second_acquire(tmp_path):
    """A second _OutputDirLock against the same dir while the first is held
    must raise OutputDirLocked. Without this, two batches against one
    output dir race on run.log and on per-PDF stem dirs."""
    lock1 = cli._OutputDirLock(tmp_path)
    lock1.__enter__()
    try:
        with pytest.raises(cli.OutputDirLocked):
            lock2 = cli._OutputDirLock(tmp_path)
            lock2.__enter__()
    finally:
        lock1.__exit__(None, None, None)


def test_output_dir_lock_releases_on_exit(tmp_path):
    """After exit, a new acquire on the same dir must succeed."""
    with cli._OutputDirLock(tmp_path):
        pass
    # Re-acquire — must not raise.
    with cli._OutputDirLock(tmp_path):
        pass


def test_main_returns_exit_locked_when_dir_held(monkeypatch, tmp_path):
    """End-to-end: main() must return EXIT_LOCKED when another instance
    holds the output-dir lock, instead of corrupting the running batch."""
    output = tmp_path / "out"
    output.mkdir()

    holder = cli._OutputDirLock(output)
    holder.__enter__()
    try:
        monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
        # F17 regression guard: a stray env var from a prior parent must
        # not let this test path bypass the lock.
        monkeypatch.delenv(cli._CHILD_HOLDS_LOCK_ENV, raising=False)
        monkeypatch.setattr(
            "sys.argv",
            ["alchemd",
             "--input-dir", str(tmp_path),
             "--output-dir", str(output),
             "-y"])
        rc = cli.main()
        assert rc == cli.EXIT_LOCKED
    finally:
        holder.__exit__(None, None, None)


def test_child_subprocess_skips_lock_when_parent_holds_it(monkeypatch, tmp_path):
    """F17 regression: the per-PDF subprocess pattern (`_process_one_subprocess`)
    spawns a child cli against the same output dir the parent holds. When the
    `_CHILD_HOLDS_LOCK_ENV` env var is set, the child must skip lock acquire
    and proceed — otherwise every PDF in subprocess (default) mode dies with
    EXIT_LOCKED before driver.process_one ever runs.

    Reproduces the 2026-04-30 S3 stress-test failure: parent holds the lock,
    child sees the same lock file, errors out instantly with no debug.log."""
    output = tmp_path / "out"
    output.mkdir()
    (tmp_path / "input").mkdir()  # empty input -> EXIT_OK after env.check

    # Parent holds the real lock (simulating the cli main being mid-batch).
    holder = cli._OutputDirLock(output)
    holder.__enter__()
    try:
        monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
        # Simulate the parent having set the bypass env var before spawning
        # this "child" invocation.
        monkeypatch.setenv(cli._CHILD_HOLDS_LOCK_ENV, "1")
        monkeypatch.setattr(
            "sys.argv",
            ["alchemd",
             "--input-dir", str(tmp_path / "input"),
             "--output-dir", str(output),
             "-y"])
        rc = cli.main()
        # Empty input dir -> EXIT_OK after the warn. Critically NOT EXIT_LOCKED.
        assert rc == cli.EXIT_OK
    finally:
        holder.__exit__(None, None, None)


def test_process_one_subprocess_sets_child_lock_env(monkeypatch, tmp_path):
    """Sanity check: `_process_one_subprocess` must propagate
    `_CHILD_HOLDS_LOCK_ENV=1` to its child. Without this, F17's bypass is
    unreachable from the actual call site."""
    captured: dict[str, str] = {}

    def fake_call(cmd, env=None, **kwargs):
        if env is not None:
            captured.update(env)
        return 1  # nonzero so caller treats it as failure (we don't care)

    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    class _Args:
        input_dir = tmp_path
        engine = "auto"
        no_sanitize = False
        keep_temp = False
        force = False
    pdf = tmp_path / "anything.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    cli._process_one_subprocess(pdf, tmp_path / "out", _Args())
    assert captured.get(cli._CHILD_HOLDS_LOCK_ENV) == "1"


# --- F2: slice mtime preservation --------------------------------------------


def test_slice_mtime_matches_source_pdf(tmp_path, monkeypatch):
    """After auto-slicing, every slice PDF must carry the source PDF's
    mtime so cached per-part outputs from a prior run still pass
    find_existing_output's `md.mtime > pdf.mtime` check.

    Reproduces F2: the rmtree+re-slice in _process_sliced was the path
    that invalidated the cache on retry."""
    # Build a tiny "PDF" file. We never invoke the real pipeline here —
    # we patch slicing.slice_pdf so the test stays fast and deterministic.
    src = tmp_path / "book.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    # Set a known mtime in the past.
    past = time.time() - 86400  # 24 hours ago
    os.utime(src, (past, past))

    fake_slice_paths = [tmp_path / f"book_part_{i:02d}.pdf" for i in range(3)]
    for sp in fake_slice_paths:
        sp.write_bytes(b"%PDF-1.4\n%%EOF\n")  # current mtime ≠ past

    def fake_slice(pdf, out_dir, chunk):
        return list(fake_slice_paths)

    monkeypatch.setattr(slicing, "slice_pdf", fake_slice)
    # Skip everything after the slice — we only want to assert the mtime.
    monkeypatch.setattr(cli, "_process_one_subprocess",
                        lambda *a, **kw: (False, "", 0.0, 0.0))

    class _Args:
        engine = "auto"
        no_sanitize = False
        keep_temp = True
        force = False
    args = _Args()

    # Invoke the slice path. It will fail (we mocked _process_one_subprocess
    # to return False) but that's OK — we only care that mtimes get stamped
    # before the per-slice processing starts.
    cli._process_sliced(src, tmp_path, args, page_count=600)

    for sp in fake_slice_paths:
        assert abs(sp.stat().st_mtime - past) < 1.0, (
            f"slice {sp.name} mtime not stamped to source: "
            f"got {sp.stat().st_mtime}, expected ~{past}")


# --- F3: run.log append (not overwrite) --------------------------------------


def test_run_log_appends_across_invocations(monkeypatch, tmp_path):
    """A second batch run must append to run.log, not overwrite. Otherwise
    prior runs' logs are silently lost."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    output = tmp_path / "out"
    # Pre-existing run.log from a "previous" run.
    output.mkdir()
    (output / "run.log").write_text(
        "# run started 2026-04-29T00:00:00Z\nfoo.pdf\tok\tmarker\tscore=1.00\n",
        encoding="utf-8")
    # Empty input dir → batch returns OK with no PDFs.
    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.setattr(
        "sys.argv",
        ["alchemd",
         "--input-dir", str(input_dir),
         "--output-dir", str(output),
         "-y"])
    rc = cli.main()
    assert rc == cli.EXIT_OK

    text = (output / "run.log").read_text(encoding="utf-8")
    assert "2026-04-29T00:00:00Z" in text, (
        "previous run's log must be preserved")
    # New batch's start line should be present too — the empty-pdfs path
    # exits early, so we don't get a new "# run started" line in this
    # specific case. Confirmed instead by re-running with a real (small)
    # workflow path: see test below.


def test_run_log_appends_on_real_no_op_run(monkeypatch, tmp_path):
    """With a non-empty PDF list, the second run must add a new
    `# run started ...` block under the previous batch's content."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    pdf = input_dir / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    output = tmp_path / "out"
    output.mkdir()
    (output / "run.log").write_text(
        "# run started 2026-04-29T00:00:00Z\nfoo.pdf\tok\tmarker\n",
        encoding="utf-8")

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.setattr(cli, "_gpu_health_probe", lambda: None)
    monkeypatch.setattr(
        cli, "_process_one_subprocess",
        lambda pdf, out, args, input_dir_override=None:
            (True, "marker", 1.0, 0.5))
    # Stub the post-processing chunking step, which expects an .md file.
    chunks_path = output / "x" / "x.md"
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    chunks_path.write_text("# ok\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        ["alchemd",
         "--input-dir", str(input_dir),
         "--output-dir", str(output),
         "--no-auto-slice", "-y"])
    rc = cli.main()
    assert rc == cli.EXIT_OK

    text = (output / "run.log").read_text(encoding="utf-8")
    assert "2026-04-29T00:00:00Z" in text, "previous run preserved"
    assert text.count("# run started") >= 2, (
        f"both runs' start markers must be present:\n{text}")


# --- F4: atomic markdown + manifest writes -----------------------------------


def test_atomic_write_uses_tmp_then_replace(tmp_path):
    """driver._atomic_write must write a sibling .tmp, then os.replace.
    A crash mid-write must NOT leave a partial file at the target path."""
    target = tmp_path / "out.md"
    driver._atomic_write(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"
    # No leftover .tmp.
    assert not (tmp_path / "out.md.tmp").exists()


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "out.md"
    target.write_text("old content", encoding="utf-8")
    driver._atomic_write(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"


# --- F10: exit code constants ------------------------------------------------


def test_exit_code_constants_are_distinct():
    """Each exit code must be a unique integer so wrappers can distinguish
    outcomes (especially CUDA-aborted vs per-PDF failures)."""
    codes = [cli.EXIT_OK, cli.EXIT_USAGE, cli.EXIT_PER_PDF_FAILURES,
             cli.EXIT_CUDA_ABORTED, cli.EXIT_LOCKED]
    assert len(set(codes)) == len(codes), f"duplicate codes: {codes}"
    assert cli.EXIT_OK == 0  # Conventional 0=success


# --- F13: UTC timestamps in driver._now() ------------------------------------


def test_driver_now_is_utc_iso8601():
    """Timestamps in debug.log must be UTC ISO-8601 (with trailing Z) so
    cross-host log correlation works."""
    s = driver._now()
    # 2026-04-30T12:34:56Z — 20 chars
    assert len(s) == 20, f"unexpected timestamp length: {s!r}"
    assert s.endswith("Z"), f"missing UTC marker: {s!r}"
    assert s[10] == "T", f"missing ISO date/time separator: {s!r}"


# --- F16: router decision rationale ------------------------------------------


def test_router_decide_with_reason_explains_marker_choice():
    profile = PdfProfile(page_count=20, is_encrypted=False, has_text_layer=1.0,
                        table_density=0.1, is_scanned=False,
                        max_table_cells=5)
    order, reason = router.decide_with_reason(profile)
    assert order[0] == "marker"
    assert "marker" in reason
    assert "table_density" in reason  # the values that didn't trip


def test_router_decide_with_reason_explains_docling_choice_max_cells():
    profile = PdfProfile(page_count=10, is_encrypted=False, has_text_layer=1.0,
                        table_density=0.1, is_scanned=False,
                        max_table_cells=200)
    order, reason = router.decide_with_reason(profile)
    assert order[0] == "docling"
    assert "max_table_cells=200" in reason
    assert ">= 50" in reason


def test_router_decide_with_reason_explains_mineru_choice_scanned():
    profile = PdfProfile(page_count=10, is_encrypted=False, has_text_layer=0.0,
                        table_density=0.0, is_scanned=True,
                        max_table_cells=0)
    order, reason = router.decide_with_reason(profile)
    assert order[0] == "mineru"
    assert "scanned" in reason


def test_router_decide_legacy_signature_still_works():
    """The original `router.decide()` signature (returning just the order)
    must keep working since callers in driver.py (rasterize-retry path)
    still use it."""
    profile = PdfProfile(page_count=10, is_encrypted=False, has_text_layer=1.0,
                        table_density=0.1, is_scanned=False,
                        max_table_cells=5)
    order = router.decide(profile)
    assert order == ["marker", "docling", "mineru"]
