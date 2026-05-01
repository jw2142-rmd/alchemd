from pathlib import Path

from alchemd import driver


class _StubEngine:
    def __init__(self, name: str, md: str = "# ok\n\ntext" * 200, fail: bool = False):
        self.name = name
        self._md = md
        self._fail = fail

    def convert(self, pdf: Path, out_dir: Path, page_count: int | None = None):
        from alchemd.engines.base import EngineError, EngineResult
        if self._fail:
            raise EngineError(self.name, "convert", "stub forced failure")
        return EngineResult(markdown=self._md, images=[], engine=self.name, elapsed=0.1)


def test_driver_writes_debug_log_on_success(clean_pdf, tmp_output):
    engines = {"marker": _StubEngine("marker"),
               "docling": _StubEngine("docling"),
               "mineru": _StubEngine("mineru")}
    result = driver.process_one(clean_pdf, tmp_output, engines=engines)
    stem_dir = tmp_output / clean_pdf.stem
    assert (stem_dir / "debug.log").exists()
    assert result.ok is True


def test_driver_retries_next_engine_on_failure(clean_pdf, tmp_output):
    engines = {"marker": _StubEngine("marker", fail=True),
               "docling": _StubEngine("docling"),
               "mineru": _StubEngine("mineru")}
    result = driver.process_one(clean_pdf, tmp_output, engines=engines)
    assert result.ok is True
    assert result.engine_used == "docling"
    log = (tmp_output / clean_pdf.stem / "debug.log").read_text(encoding="utf-8")
    assert "marker" in log and "docling" in log


def test_driver_writes_debug_log_on_total_failure(clean_pdf, tmp_output):
    engines = {"marker": _StubEngine("marker", fail=True),
               "docling": _StubEngine("docling", fail=True),
               "mineru": _StubEngine("mineru", fail=True)}
    result = driver.process_one(clean_pdf, tmp_output, engines=engines)
    assert result.ok is False
    log = (tmp_output / clean_pdf.stem / "debug.log").read_text(encoding="utf-8")
    assert "failed" in log.lower()


def test_driver_rasterize_retries_even_if_some_engines_fail_differently(
        tmp_path, monkeypatch):
    """Regression: force_rasterize retry must fire when ANY engine hits
    page-load damage, not only when ALL do. In the first overnight re-run,
    marker+docling both raised PdfiumError("Failed to load page") but mineru
    failed with CUDA OOM; the old condition (page_load >= attempted_engines)
    required all 3 to match, so the retry never fired."""
    from alchemd import sanitize, preflight
    from alchemd.engines.base import EngineError

    # Sanitize: first call succeeds (tier=ghostscript), force_rasterize
    # succeeds too (tier=rasterize). Record which was called.
    calls: list[bool] = []

    def fake_sanitize(src, force_rasterize=False, on_event=None):
        calls.append(force_rasterize)
        from alchemd.sanitize import SanitizeResult
        if on_event:
            on_event(f"sanitize call force_rasterize={force_rasterize}")
        return SanitizeResult(
            ok=True, path=src, is_temp=False,
            tier="rasterize" if force_rasterize else "ghostscript",
            elapsed=0.1, events=[])

    monkeypatch.setattr(sanitize, "run", fake_sanitize)

    # Preflight: pretend any PDF is fine.
    def fake_probe(pdf, original_pdf=None):
        from alchemd.preflight import PdfProfile
        return PdfProfile(page_count=10, is_encrypted=False,
                          has_text_layer=1.0, table_density=0.0,
                          is_scanned=False)

    monkeypatch.setattr(preflight, "probe", fake_probe)

    # Two engines, different failure modes:
    #   marker  -> page-load (matches markers)
    #   mineru  -> CUDA OOM  (does NOT match markers)
    # After the FIRST attempt: retry with force_rasterize must fire because
    # page_load >= 1 — not because >= 3.
    attempt = {"count": 0}

    class _FailThenSucceed:
        def __init__(self, name, first_err, succeed_on_retry):
            self.name = name
            self.first_err = first_err
            self.succeed_on_retry = succeed_on_retry

        def convert(self, pdf, out_dir, page_count=None):
            attempt["count"] += 1
            if self.succeed_on_retry and calls[-1] is True:
                from alchemd.engines.base import EngineResult
                return EngineResult(
                    markdown="# ok\n\n" + "word " * 500, images=[],
                    engine=self.name, elapsed=0.1)
            raise EngineError(self.name, "convert", self.first_err)

    engines = {
        "marker": _FailThenSucceed("marker", "Failed to load page.", True),
        "docling": _FailThenSucceed("docling", "Failed to load page.", True),
        "mineru": _FailThenSucceed(
            "mineru", "CUDA out of memory. Tried to allocate 3.16 GiB", True),
    }

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"
    result = driver.process_one(pdf, out, engines=engines)

    assert result.ok is True, (
        f"rasterize-retry should have recovered; got reason={result.reason}")
    # First sanitize call = tier-1, second = force_rasterize
    assert calls == [False, True], (
        f"expected tier-1 then force_rasterize retry, got {calls}")


def test_driver_streams_sanitize_events_live(tmp_path, monkeypatch):
    """Regression: sanitize events must be written through to debug.log as they
    happen, so a hard crash mid-sanitize leaves a breadcrumb. Previously
    events were returned on the SanitizeResult and only written on normal
    return — interrupted sanitize left an empty log after '=== sanitize ==='."""
    from alchemd import sanitize

    seen_log_paths: list[Path] = []

    def fake_run(src, force_rasterize=False, on_event=None):
        # Emit an event, then simulate a hard crash BEFORE returning. The
        # assertion is that the emitted event already landed on disk.
        if on_event is not None:
            on_event("ghostscript: start src=probe.pdf")
        # Check the debug log on disk RIGHT NOW, before we return.
        for p in seen_log_paths:
            text = p.read_text(encoding="utf-8")
            if "ghostscript: start src=probe.pdf" not in text:
                raise AssertionError(
                    f"event not yet streamed to disk when emit returned:\n{text}")
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(sanitize, "run", fake_run)

    # Intercept _DebugLog to capture its path.
    orig_init = driver._DebugLog.__init__

    def spy_init(self, path):
        orig_init(self, path)
        seen_log_paths.append(path)

    monkeypatch.setattr(driver._DebugLog, "__init__", spy_init)

    bad = tmp_path / "probe.pdf"
    bad.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"
    try:
        driver.process_one(bad, out, engines={})
    except RuntimeError:
        pass  # crash is simulated

    assert seen_log_paths, "debug log path must have been captured"
    text = seen_log_paths[0].read_text(encoding="utf-8")
    assert "ghostscript: start src=probe.pdf" in text


def test_driver_writes_status_failed_when_sanitize_fails(tmp_path, monkeypatch):
    """Regression: the sanitize-failure branch must write a 'done' section
    with 'status=failed' line, consistent with every other failure path. A
    missing status line made run.log failures hard to correlate to debug.log."""
    from alchemd import sanitize

    def fake_run(src, force_rasterize=False, on_event=None):
        if on_event is not None:
            on_event("ghostscript: forced failure")
            on_event("rasterize: forced failure")
        from alchemd.sanitize import SanitizeResult
        return SanitizeResult(ok=False, path=src, is_temp=False, tier="",
                              elapsed=0.1,
                              events=["ghostscript: forced failure",
                                      "rasterize: forced failure"])

    monkeypatch.setattr(sanitize, "run", fake_run)

    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"
    result = driver.process_one(bad, out, engines={})

    assert result.ok is False
    log_text = (out / "bad" / "debug.log").read_text(encoding="utf-8")
    assert "status=failed" in log_text, (
        "sanitize-failure branch must emit a status=failed line for "
        "run.log correlation")


def test_driver_propagates_cuda_poisoned_error_with_failure_trailer(tmp_path):
    """Regression: when an engine raises EngineError(stage='cuda_poisoned'),
    driver must (1) write the standard 'status=failed reason=cuda_poisoned:'
    trailer into debug.log so the cli's debug-log scrape on the subprocess
    path can detect it, AND (2) re-raise the EngineError so the in-process
    cli path can convert it to CudaPoisonedError. Catching it as a regular
    failure would silently demote a batch-aborting condition to a per-PDF
    failure, queuing the next PDFs against a poisoned GPU (the 2026-04-29
    incident: 4-hour marker hangs on each subsequent PDF)."""
    from alchemd import preflight, sanitize
    from alchemd.engines.base import EngineError
    from alchemd.sanitize import SanitizeResult

    class _CudaPoisonEngine:
        name = "marker"

        def convert(self, pdf, out_dir, page_count=None):
            raise EngineError(
                self.name, "cuda_poisoned",
                "GPU CUDA context poisoned (driver in dead state)")

    def fake_sanitize(src, force_rasterize=False, on_event=None):
        return SanitizeResult(
            ok=True, path=src, is_temp=False, tier="ghostscript",
            elapsed=0.1, events=[])

    def fake_probe(pdf, original_pdf=None):
        from alchemd.preflight import PdfProfile
        return PdfProfile(page_count=10, is_encrypted=False,
                          has_text_layer=1.0, table_density=0.0,
                          is_scanned=False)

    import pytest
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    import unittest.mock as mock
    with mock.patch.object(sanitize, "run", fake_sanitize), \
         mock.patch.object(preflight, "probe", fake_probe):
        with pytest.raises(EngineError) as exc_info:
            driver.process_one(pdf, out, engines={"marker": _CudaPoisonEngine()})

    assert exc_info.value.stage == "cuda_poisoned"
    log = (out / "x" / "debug.log").read_text(encoding="utf-8")
    assert "status=failed" in log
    assert "cuda_poisoned" in log, (
        "trailer must contain 'cuda_poisoned' so the cli's debug.log scrape "
        "on the subprocess path can detect and abort the batch")


def test_driver_sanitizes_before_preflight(tmp_path):
    """Regression: a malformed PDF must hit sanitize first, not preflight.
    pypdfium2-unreadable originals previously failed at preflight and never
    reached the sanitize stage."""
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot actually a pdf\n%%EOF\n")
    out = tmp_path / "out"

    result = driver.process_one(bad, out, engines={})
    assert result.ok is False
    log_text = (out / "bad" / "debug.log").read_text(encoding="utf-8")

    s_idx = log_text.find("=== sanitize ===")
    p_idx = log_text.find("=== preflight ===")
    assert s_idx != -1, "sanitize section must appear in the debug log"
    if p_idx != -1:
        assert s_idx < p_idx, "sanitize must run before preflight"
