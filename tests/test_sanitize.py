import pypdfium2 as pdfium
from pathlib import Path

from alchemd import sanitize


def _loadable(p: Path) -> bool:
    try:
        d = pdfium.PdfDocument(str(p))
        n = len(d)
        d.close()
        return n > 0
    except Exception:
        return False


def test_sanitize_ghostscript_outputs_loadable_pdf(clean_pdf, tmp_path):
    out = tmp_path / "gs.pdf"
    sanitize.ghostscript(clean_pdf, out, timeout=600)
    assert _loadable(out)


def test_sanitize_rasterize_outputs_loadable_pdf(clean_pdf, tmp_path):
    out = tmp_path / "ras.pdf"
    sanitize.rasterize(clean_pdf, out, dpi=100)
    assert _loadable(out)


def test_run_returns_path_to_loadable_sanitized_copy(clean_pdf):
    result = sanitize.run(clean_pdf)
    try:
        assert result.ok
        assert result.tier in ("ghostscript", "rasterize")
        assert _loadable(result.path)
    finally:
        if result.is_temp and result.path != clean_pdf:
            result.path.unlink(missing_ok=True)


def test_run_falls_back_to_rasterize_when_gs_fails(clean_pdf, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("gs forced failure")
    monkeypatch.setattr(sanitize, "ghostscript", boom)

    result = sanitize.run(clean_pdf)
    try:
        assert result.ok
        assert result.tier == "rasterize"
    finally:
        if result.is_temp and result.path != clean_pdf:
            result.path.unlink(missing_ok=True)


def test_run_streams_events_via_on_event_callback(monkeypatch, tmp_path):
    """Regression: sanitize events must reach the log incrementally, not only
    on return. If sanitize.run() is interrupted mid-flight (process killed,
    KeyboardInterrupt), events collected but not yet returned are lost —
    leaving an empty debug.log after '=== sanitize ==='.

    Drives run() with both tiers forced to fail so no real PDF is needed."""
    def boom(*a, **kw):
        raise RuntimeError("forced")
    monkeypatch.setattr(sanitize, "ghostscript", boom)
    monkeypatch.setattr(sanitize, "rasterize", boom)

    streamed: list[str] = []
    src = tmp_path / "dummy.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = sanitize.run(src, on_event=streamed.append)

    assert streamed, "on_event must have been called during sanitize"
    assert streamed == result.events, (
        "streamed events must match the events returned on the result "
        "(backward-compat guarantee)")


def test_rasterize_uses_pdfimage_device_not_png_plus_img2pdf(monkeypatch, tmp_path):
    """Regression: rasterize used to write per-page PNGs via gs -sDEVICE=png16m
    and pack them with img2pdf. img2pdf's output failed pypdfium2 verify on
    page 0 for 449- and 377-page books (a_non_random_walk, the_master_swing_trader),
    blocking the rasterize-retry path. Replaced with a single gs -sDEVICE=pdfimage24
    pass that writes a structurally-clean rasterized PDF directly. Guard against
    regression to the PNG+img2pdf pattern."""
    captured_cmds: list[list] = []

    def fake_run(cmd, *args, **kw):
        captured_cmds.append(list(cmd))
        out_arg = next(c for c in cmd if str(c).startswith("-sOutputFile="))
        out_path = out_arg.split("=", 1)[1]
        Path(out_path).write_bytes(b"%PDF-fake")
        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    monkeypatch.setattr(sanitize.subprocess, "run", fake_run)
    monkeypatch.setattr(sanitize.env, "find_ghostscript", lambda: "gs")

    out = tmp_path / "ras.pdf"
    sanitize.rasterize(tmp_path / "src.pdf", out, dpi=100)

    assert captured_cmds, "rasterize must invoke ghostscript"
    cmd = captured_cmds[0]
    assert "-sDEVICE=pdfimage24" in cmd, (
        f"rasterize must use -sDEVICE=pdfimage24 (one-shot rasterized PDF); "
        f"got: {cmd}")
    assert not any("png16m" in str(c) for c in cmd), (
        "rasterize must not use -sDEVICE=png16m + img2pdf — that pattern "
        "produced pypdfium2-unloadable output on large books")
    assert len(captured_cmds) == 1, (
        f"rasterize should be a single gs call (no img2pdf step); "
        f"got {len(captured_cmds)} subprocess calls")


def test_ghostscript_timeout_scales_with_file_size(monkeypatch, tmp_path):
    """Regression: a fixed 600s timeout was too short for very large books
    (observed: 255MB principles_of_neurology.pdf hit the timeout). Timeout
    must scale with file size since speed is not a constraint per spec."""
    captured_timeouts: list[int] = []

    def fake_run(cmd, *args, **kw):
        captured_timeouts.append(kw.get("timeout"))
        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    monkeypatch.setattr(sanitize.subprocess, "run", fake_run)
    monkeypatch.setattr(sanitize.env, "find_ghostscript", lambda: "gs")

    small = tmp_path / "small.pdf"
    small.write_bytes(b"x" * (1 * 1024 * 1024))  # 1 MB
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (255 * 1024 * 1024))  # 255 MB

    sanitize.ghostscript(small, tmp_path / "a.pdf")
    sanitize.ghostscript(big, tmp_path / "b.pdf")

    assert captured_timeouts[0] == 600, (
        f"small PDF should get baseline 600s timeout, got {captured_timeouts[0]}")
    assert captured_timeouts[1] > 600 * 5, (
        f"255MB PDF must get substantially more than baseline, "
        f"got {captured_timeouts[1]}s")
    assert captured_timeouts[1] <= 4 * 3600, (
        f"timeout should be capped at 4h to avoid runaway, "
        f"got {captured_timeouts[1]}s")


def test_verify_rejects_pdf_whose_pages_fail_to_render(monkeypatch, tmp_path):
    """Regression: Ghostscript can produce a PDF whose header loads but whose
    pages fail to render. Downstream engines then reject it silently. _verify
    must catch that so the rasterize tier kicks in."""
    class _FakePage:
        def render(self, scale=1.0):
            raise RuntimeError("page render boom")
        def close(self):
            pass
    class _FakeDoc:
        def __len__(self):
            return 3
        def __getitem__(self, i):
            return _FakePage()
        def close(self):
            pass

    import pypdfium2
    monkeypatch.setattr(pypdfium2, "PdfDocument", lambda *a, **k: _FakeDoc())
    ok, reason = sanitize._verify(tmp_path / "anything.pdf")
    assert ok is False
    assert "render" in reason
