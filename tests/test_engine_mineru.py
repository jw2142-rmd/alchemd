import os
import pytest
from alchemd.engines.base import EngineError
from alchemd.engines import mineru as m


def test_mineru_engine_name():
    assert m.MinerUEngine().name == "mineru"


def test_mineru_uses_method_auto_not_ocr(tmp_path, monkeypatch):
    """Regression: --method ocr forced OCR on every page of text-layer PDFs,
    causing CUDA OOM (observed 3.16 GiB allocation request on a 449-page
    book). auto lets mineru use the text layer when present and OCR only
    when needed."""
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    engine = m.MinerUEngine(binary="mineru")
    out = tmp_path / "out"
    out.mkdir()
    # .md needed so convert() doesn't error on missing output — plant one.
    work = out / "mineru_work"
    work.mkdir()
    (work / "x.md").write_text("ok")

    engine.convert(tmp_path / "x.pdf", out)

    cmd = captured["cmd"]
    assert "--method" in cmd
    m_idx = cmd.index("--method")
    assert cmd[m_idx + 1] == "auto", (
        f"expected --method auto to avoid forcing OCR on text-layer PDFs, "
        f"got --method {cmd[m_idx + 1]}")


def test_mineru_sets_cuda_alloc_conf_env(tmp_path, monkeypatch):
    """Regression: mineru hit fragmentation OOM even when enough GPU memory
    was free ('12.06 GiB free' but failed to allocate 3.16 GiB). The hint
    is PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True — set it so the
    subprocess inherits a friendlier allocator config."""
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        captured["env"] = kwargs.get("env")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    engine = m.MinerUEngine(binary="mineru")
    out = tmp_path / "out"
    out.mkdir()
    work = out / "mineru_work"
    work.mkdir()
    (work / "x.md").write_text("ok")

    engine.convert(tmp_path / "x.pdf", out)

    env = captured["env"]
    assert env is not None, (
        "mineru must pass an explicit env so PYTORCH_CUDA_ALLOC_CONF "
        "propagates even when the parent hasn't set it")
    assert "expandable_segments:True" in env.get("PYTORCH_CUDA_ALLOC_CONF", "")


def test_mineru_converts_scanned_pdf(scanned_pdf, tmp_output):
    try:
        result = m.MinerUEngine().convert(scanned_pdf, tmp_output)
    except EngineError as exc:
        if "CLI not found" in str(exc):
            pytest.skip("mineru CLI not installed")
        raise
    assert result.engine == "mineru"
    assert len(result.markdown.strip()) > 0
