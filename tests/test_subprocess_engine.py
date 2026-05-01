"""SubprocessEngine wraps engine_runner. These tests fake subprocess.run so
they exercise the wrapper's payload handling, not actual engine code."""
import json
import subprocess
from pathlib import Path

import pytest

from alchemd.engines.base import EngineError
from alchemd.engines.subprocess_engine import (
    DEFAULT_TIMEOUT_SEC,
    SubprocessEngine,
    adaptive_timeout,
)


def _fake_run_that_writes_payload(result_path: Path, payload: dict,
                                  returncode: int = 0,
                                  stderr: bytes = b""):
    """Build a fake subprocess.run that imitates a child that wrote result_path."""
    def fake_run(cmd, *a, **kw):
        # The wrapper passes --result-path <path>
        idx = cmd.index("--result-path")
        rp = Path(cmd[idx + 1])
        rp.write_text(json.dumps(payload), encoding="utf-8")

        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stdout = b""
        r.stderr = stderr
        return r
    return fake_run


def test_returns_engine_result_on_success_payload(tmp_path, monkeypatch):
    md_file = tmp_path / ".marker_raw.md"
    md_file.write_text("# marker output", encoding="utf-8")
    payload = {
        "ok": True, "engine": "marker", "markdown_path": str(md_file),
        "images": [], "elapsed": 12.3, "notes": ["hi"],
    }
    monkeypatch.setattr(subprocess, "run",
                        _fake_run_that_writes_payload(tmp_path, payload))

    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = engine.convert(pdf, tmp_path / "out")

    assert result.engine == "marker"
    assert result.markdown == "# marker output"
    assert result.elapsed == 12.3
    assert result.notes == ["hi"]
    # Wrapper must clean up the raw markdown file after reading it (otherwise
    # repeated attempts accumulate stale files in the stem dir).
    assert not md_file.exists()


def test_raises_engine_error_on_failure_payload(tmp_path, monkeypatch):
    payload = {
        "ok": False, "engine": "marker", "error_type": "EngineError",
        "error": "convert blew up", "stage": "convert",
        "traceback": "...",
    }
    monkeypatch.setattr(subprocess, "run",
                        _fake_run_that_writes_payload(tmp_path, payload,
                                                      returncode=2))
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    assert "convert blew up" in str(exc_info.value)
    assert exc_info.value.stage == "convert"


def test_raises_engine_error_when_child_writes_no_payload(tmp_path, monkeypatch):
    """If the child dies before writing the payload (OOM kill, segfault),
    the parent must raise with both stdout AND stderr tails so debug.log
    shows WHY. Some crashes (pypdfium2 C++ aborts, 'Fatal Python error' on
    Windows) emit on stdout — stderr-only loses the actual cause."""
    def fake_run(cmd, *a, **kw):
        class _R:
            returncode = -9  # SIGKILL-ish
            stdout = b"Fatal Python error: Aborted"
            stderr = b"CUDA out of memory"
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    msg = str(exc_info.value)
    assert "no payload written" in msg
    assert "CUDA out of memory" in msg
    assert "Fatal Python error: Aborted" in msg


def test_raises_engine_error_on_timeout(tmp_path, monkeypatch):
    def boom(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", boom)
    engine = SubprocessEngine("marker", timeout=5)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    assert "timeout" in str(exc_info.value).lower()


def test_classifies_cuda_oom_in_failure_payload_as_cuda_poisoned(tmp_path, monkeypatch):
    """The 2026-04-29 incident: docling wrote a structured failure payload
    (stage='convert') whose error string contained 'CUDA error: out of
    memory'. The wrapper must reclassify these as stage='cuda_poisoned' so
    the driver short-circuits the chain and the cli aborts the batch — the
    GPU is dead until reboot."""
    payload = {
        "ok": False, "engine": "docling", "error_type": "EngineError",
        "error": ("[docling:convert] Conversion failed for: foo.pdf with "
                  "status: ConversionStatus.FAILURE. Errors: Page 1: "
                  "CUDA error: out of memory; Page 2: CUDA error: invalid "
                  "resource handle"),
        "stage": "convert",
        "traceback": "...",
    }
    monkeypatch.setattr(subprocess, "run",
                        _fake_run_that_writes_payload(tmp_path, payload, returncode=2))
    engine = SubprocessEngine("docling")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    assert exc_info.value.stage == "cuda_poisoned"
    assert "reboot required" in str(exc_info.value)


def test_classifies_torch_cuda_outofmemoryerror_as_cuda_poisoned(tmp_path, monkeypatch):
    payload = {
        "ok": False, "engine": "marker", "error_type": "OutOfMemoryError",
        "error": "torch.cuda.OutOfMemoryError: CUDA out of memory.",
        "stage": "convert",
        "traceback": "...",
    }
    monkeypatch.setattr(subprocess, "run",
                        _fake_run_that_writes_payload(tmp_path, payload, returncode=2))
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    assert exc_info.value.stage == "cuda_poisoned"


def test_classifies_cuda_error_in_dead_subprocess_stderr(tmp_path, monkeypatch):
    """Subprocess died without payload AND stderr mentions a CUDA error —
    classifier must prefer cuda_poisoned over the generic 'subprocess'
    or memory_pressure paths."""
    def fake_run(cmd, *a, **kw):
        class _R:
            returncode = -9
            stdout = b""
            stderr = b"CUDA error: unknown error"
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    assert exc_info.value.stage == "cuda_poisoned"
    assert "reboot required" in str(exc_info.value)


def test_memory_pressure_still_reports_pagefile_failure(tmp_path, monkeypatch):
    """After moving CUDA markers out of memory_pressure, the OpenBLAS /
    Windows-pagefile signatures must still classify as memory_pressure
    (recoverable without a reboot — close other RAM users instead)."""
    def fake_run(cmd, *a, **kw):
        class _R:
            returncode = 1
            stdout = b""
            stderr = b"OpenBLAS error: Memory allocation still failed"
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(EngineError) as exc_info:
        engine.convert(pdf, tmp_path / "out")
    assert exc_info.value.stage == "memory_pressure"


def test_subprocess_engine_sets_cuda_alloc_conf(tmp_path, monkeypatch):
    """Every engine subprocess must inherit PYTORCH_CUDA_ALLOC_CONF=
    expandable_segments:True — the documented mitigation for fragmentation
    OOMs that was previously only set in mineru.py."""
    captured_env: dict = {}
    def fake_run(cmd, *a, **kw):
        captured_env.update(kw.get("env") or {})
        idx = cmd.index("--result-path")
        rp = Path(cmd[idx + 1])
        rp.write_text(json.dumps({
            "ok": True, "engine": "marker",
            "markdown_path": str(tmp_path / "x.md"),
            "images": [], "elapsed": 0.1, "notes": [],
        }), encoding="utf-8")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    (tmp_path / "x.md").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    engine.convert(pdf, tmp_path / "out")
    assert captured_env.get("PYTORCH_CUDA_ALLOC_CONF") == "expandable_segments:True"


def test_adaptive_timeout_grows_with_page_count():
    # Tiny paper: baseline + 30 s/page = 60 + 50*30 = 1560 s (~26 min)
    assert adaptive_timeout(50) == 60 + 30 * 50
    # Large book: capped at the engine cap, not 60 + 1500*30
    assert adaptive_timeout(1500) == DEFAULT_TIMEOUT_SEC
    # None / zero falls back to the cap (legacy behaviour)
    assert adaptive_timeout(None) == DEFAULT_TIMEOUT_SEC
    assert adaptive_timeout(0) == DEFAULT_TIMEOUT_SEC
    # Custom cap honored when smaller than the formula
    assert adaptive_timeout(50, cap=120) == 120


def test_subprocess_engine_uses_adaptive_timeout(tmp_path, monkeypatch):
    """Driver passes page_count from preflight; SubprocessEngine must apply
    the adaptive formula instead of the 4-hour cap. A 50p paper hung for
    4 hours on the 2026-04-29 incident — adaptive bounds it to ~26 min."""
    captured_timeout: dict = {}
    def fake_run(cmd, *a, **kw):
        captured_timeout["t"] = kw.get("timeout")
        idx = cmd.index("--result-path")
        rp = Path(cmd[idx + 1])
        rp.write_text(json.dumps({
            "ok": True, "engine": "marker",
            "markdown_path": str(tmp_path / "x.md"),
            "images": [], "elapsed": 0.1, "notes": [],
        }), encoding="utf-8")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    (tmp_path / "x.md").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    engine.convert(pdf, tmp_path / "out", page_count=50)
    assert captured_timeout["t"] == 60 + 30 * 50  # 1560 s


def test_subprocess_engine_falls_back_to_cap_without_page_count(tmp_path, monkeypatch):
    captured_timeout: dict = {}
    def fake_run(cmd, *a, **kw):
        captured_timeout["t"] = kw.get("timeout")
        idx = cmd.index("--result-path")
        rp = Path(cmd[idx + 1])
        rp.write_text(json.dumps({
            "ok": True, "engine": "marker",
            "markdown_path": str(tmp_path / "x.md"),
            "images": [], "elapsed": 0.1, "notes": [],
        }), encoding="utf-8")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    (tmp_path / "x.md").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", fake_run)
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    engine.convert(pdf, tmp_path / "out")  # no page_count
    assert captured_timeout["t"] == DEFAULT_TIMEOUT_SEC


def test_user_pytorch_cuda_alloc_conf_override_wins(tmp_path, monkeypatch):
    """If the operator already exported PYTORCH_CUDA_ALLOC_CONF (e.g. to
    debug a different fragmentation strategy), the SubprocessEngine must
    not clobber it. setdefault semantics."""
    captured_env: dict = {}
    def fake_run(cmd, *a, **kw):
        captured_env.update(kw.get("env") or {})
        idx = cmd.index("--result-path")
        rp = Path(cmd[idx + 1])
        rp.write_text(json.dumps({
            "ok": True, "engine": "marker",
            "markdown_path": str(tmp_path / "x.md"),
            "images": [], "elapsed": 0.1, "notes": [],
        }), encoding="utf-8")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""
        return _R()

    (tmp_path / "x.md").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PYTORCH_CUDA_ALLOC_CONF", "garbage_collection_threshold:0.6")
    engine = SubprocessEngine("marker")
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    engine.convert(pdf, tmp_path / "out")
    assert captured_env.get("PYTORCH_CUDA_ALLOC_CONF") == "garbage_collection_threshold:0.6"
