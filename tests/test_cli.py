import json
from pathlib import Path

import pytest

from alchemd import cli
from alchemd.engines.base import EngineError


def test_build_parser_defaults():
    p = cli.build_parser()
    args = p.parse_args([])
    assert args.pdf is None
    assert args.force is False
    assert args.engine == "auto"


def test_build_parser_custom_engine():
    p = cli.build_parser()
    args = p.parse_args(["--engine", "docling"])
    assert args.engine == "docling"


def test_main_rejects_directory_passed_as_pdf(monkeypatch, tmp_path, capsys):
    """Directory paths slip through the old `pdf_path.exists()` check and produce
    a misleading 'sanitize: all tiers failed' downstream. Reject early."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "subdir").mkdir()  # the user's positional arg
    out = tmp_path / "out"
    monkeypatch.setattr("sys.argv",
                        ["alchemd", "subdir",
                         "--input-dir", str(input_dir),
                         "--output-dir", str(out), "-y"])
    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    rc = cli.main()
    assert rc == 1
    err = capsys.readouterr().out + capsys.readouterr().err
    assert "Not a PDF" in err or "not a pdf" in err.lower()


def test_main_rejects_non_pdf_file(monkeypatch, tmp_path, capsys):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "notes.txt").write_text("hi")
    out = tmp_path / "out"
    monkeypatch.setattr("sys.argv",
                        ["alchemd", "notes.txt",
                         "--input-dir", str(input_dir),
                         "--output-dir", str(out), "-y"])
    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    rc = cli.main()
    assert rc == 1


# --- CUDA-poisoning propagation tests --------------------------------------
# These cover the L3 chain that has no other coverage: driver re-raise →
# cli converts EngineError(stage='cuda_poisoned') → batch abort with exit 3.
# White-box review on 2026-04-30 found the L2 markers were well-tested at the
# subprocess_engine wrapper, but every layer above it (gpu probe, in-process
# classify, subprocess debug.log scrape, exit code) was uncovered.


def test_in_process_converts_cuda_poisoned_engine_error(monkeypatch, tmp_path):
    """Regression: _process_one_in_process must re-raise an
    EngineError(stage='cuda_poisoned') as CudaPoisonedError. Without this
    wrapping, the main loop would treat it as a regular EngineError and
    move on to the next PDF — re-poisoning every subsequent attempt."""
    from alchemd import driver

    def fake_process_one(pdf, out, engines, skip_sanitize=False):
        raise EngineError("marker", "cuda_poisoned",
                          "GPU CUDA context poisoned")

    monkeypatch.setattr(driver, "process_one", fake_process_one)

    class _Args:
        engine = "auto"
        no_sanitize = False

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    with pytest.raises(cli.CudaPoisonedError):
        cli._process_one_in_process(pdf, out, _Args(), engines={})


def test_in_process_passes_through_non_cuda_engine_error(monkeypatch, tmp_path):
    """Regression: only stage='cuda_poisoned' should become CudaPoisonedError.
    A regular convert-stage EngineError must propagate as-is so the per-PDF
    failure path keeps working."""
    from alchemd import driver

    def fake_process_one(pdf, out, engines, skip_sanitize=False):
        raise EngineError("marker", "convert", "boom")

    monkeypatch.setattr(driver, "process_one", fake_process_one)

    class _Args:
        engine = "auto"
        no_sanitize = False

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    with pytest.raises(EngineError) as exc:
        cli._process_one_in_process(pdf, out, _Args(), engines={})
    assert exc.value.stage == "convert"


def test_gpu_health_probe_silent_when_torch_unavailable(monkeypatch):
    """Probe must return without raising if torch isn't importable. Boxes
    without GPU still need to run the cli; the probe is optional.

    Setting sys.modules['torch'] to None makes `import torch` raise
    ImportError without unloading the real torch module — popping it
    triggers torch's class-level TORCH_LIBRARY('triton') re-registration
    on next import, which crashes subsequent tests."""
    import sys
    monkeypatch.setitem(sys.modules, "torch", None)
    cli._gpu_health_probe()  # must not raise


def test_gpu_health_probe_silent_when_cuda_unavailable(monkeypatch):
    """Probe must return without raising if CUDA isn't initialized."""
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    cli._gpu_health_probe()  # must not raise


def test_gpu_health_probe_raises_on_cuda_failure(monkeypatch):
    """Probe must raise CudaPoisonedError if a tiny CUDA op fails. This is
    the L3 between-PDF guard: a poisoned driver from a prior run (or another
    job on the box) gets caught in <1 s instead of hanging the next engine
    subprocess for 4 hours."""
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    def boom(*a, **kw):
        raise RuntimeError("CUDA error: invalid resource handle")

    monkeypatch.setattr(torch, "zeros", boom)

    with pytest.raises(cli.CudaPoisonedError) as exc:
        cli._gpu_health_probe()
    assert "poisoned" in str(exc.value).lower()


def test_main_aborts_batch_with_exit_3_on_in_process_cuda_poisoned(
        monkeypatch, tmp_path, capsys):
    """End-to-end regression: a CudaPoisonedError raised by the in-process
    path under --no-cpu-fallback must abort the rest of the batch and return
    exit code 3 (distinct from 0=ok, 1=arg error, 2=per-PDF failures).
    The default mode now CPU-retries instead — see
    test_main_cpu_fallback_recovers_cuda_poisoned_in_process below."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    a = input_dir / "a.pdf"
    b = input_dir / "b.pdf"
    a.write_bytes(b"%PDF-1.4\n%%EOF\n")
    b.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    # Skip the GPU health probe in this test — we're exercising the
    # post-engine-failure path, not the probe path.
    monkeypatch.setattr(cli, "_gpu_health_probe", lambda: None)

    processed: list[str] = []

    def fake_in_process(pdf, out, args, engines=None):
        processed.append(pdf.name)
        raise cli.CudaPoisonedError(
            "GPU CUDA context poisoned (driver in dead state)")

    monkeypatch.setattr(cli, "_process_one_in_process", fake_in_process)

    monkeypatch.setattr(
        "sys.argv",
        ["alchemd",
         "--input-dir", str(input_dir),
         "--output-dir", str(out),
         "--in-process",
         "--no-auto-slice",
         "--no-cpu-fallback",
         "-y"])

    rc = cli.main()
    assert rc == 3, f"expected exit code 3 (cuda_aborted), got {rc}"
    assert processed == ["a.pdf"], (
        f"second PDF must NOT be attempted after cuda_poisoned; "
        f"got {processed}")
    run_log = (out / "run.log").read_text(encoding="utf-8")
    assert "ABORTED" in run_log
    assert "cuda_poisoned" in run_log


def test_main_cpu_fallback_recovers_cuda_poisoned_in_process(
        monkeypatch, tmp_path):
    """Default mode: in-process CudaPoisonedError engages CPU fallback,
    retries the failing PDF, and continues the batch. args.cpu flips True
    so subsequent PDFs spawn with the GPU hidden via CUDA_VISIBLE_DEVICES=-1.
    """
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    a = input_dir / "a.pdf"
    b = input_dir / "b.pdf"
    a.write_bytes(b"%PDF-1.4\n%%EOF\n")
    b.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.setattr(cli, "_gpu_health_probe", lambda: None)

    calls: list[tuple[str, bool]] = []

    def fake_in_process(pdf, out_dir, args, engines=None):
        calls.append((pdf.name, args.cpu))
        # First call (GPU mode) on a.pdf poisons; every subsequent call
        # (CPU mode) succeeds.
        if len(calls) == 1:
            raise cli.CudaPoisonedError(
                "GPU CUDA context poisoned (driver in dead state)")
        # Write a fake md so chunking has something to chunk.
        stem_dir = out_dir / pdf.stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        (stem_dir / f"{pdf.stem}.md").write_text("# ok\n", encoding="utf-8")
        (stem_dir / "manifest.json").write_text("{}", encoding="utf-8")
        return (True, "marker", 1.0, 0.1)

    monkeypatch.setattr(cli, "_process_one_in_process", fake_in_process)

    monkeypatch.setattr(
        "sys.argv",
        ["alchemd",
         "--input-dir", str(input_dir),
         "--output-dir", str(out),
         "--in-process",
         "--no-auto-slice",
         "-y"])

    rc = cli.main()
    # Exit 0: a.pdf failed once but the CPU retry recovered, b.pdf ran on CPU.
    assert rc == 0, f"expected exit 0 after CPU recovery, got {rc}"
    # Calls: a.pdf GPU (poisoned) → a.pdf CPU (ok) → b.pdf CPU (ok)
    assert calls == [
        ("a.pdf", False),
        ("a.pdf", True),
        ("b.pdf", True),
    ], f"unexpected call sequence: {calls}"


def test_main_aborts_batch_with_exit_3_via_subprocess_debug_log_scrape(
        monkeypatch, tmp_path):
    """Subprocess-isolation path with --no-cpu-fallback: when each PDF runs
    in a child interpreter, CudaPoisonedError can't propagate by exception —
    the main loop must scrape debug.log for the cuda_poisoned reason and
    abort. Default mode now CPU-retries instead — covered by
    test_main_cpu_fallback_recovers_subprocess_cuda_poisoned below."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    a = input_dir / "a.pdf"
    b = input_dir / "b.pdf"
    a.write_bytes(b"%PDF-1.4\n%%EOF\n")
    b.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.setattr(cli, "_gpu_health_probe", lambda: None)

    processed: list[str] = []

    def fake_subprocess(pdf, out_dir, args, input_dir_override=None):
        processed.append(pdf.name)
        # Simulate the child writing the standard cuda_poisoned trailer
        # into debug.log — which is what driver.process_one does on the
        # cuda_poisoned path before re-raising.
        stem_dir = out_dir / pdf.stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        (stem_dir / "debug.log").write_text(
            "[2026-04-30 12:00:00] === done ===\n"
            "[2026-04-30 12:00:00] status=failed "
            "reason=cuda_poisoned: GPU CUDA context poisoned\n",
            encoding="utf-8")
        return (False, "", 0.0, 0.0)

    monkeypatch.setattr(cli, "_process_one_subprocess", fake_subprocess)

    monkeypatch.setattr(
        "sys.argv",
        ["alchemd",
         "--input-dir", str(input_dir),
         "--output-dir", str(out),
         "--no-auto-slice",
         "--no-cpu-fallback",
         "-y"])

    rc = cli.main()
    assert rc == 3, f"expected exit code 3 (cuda_aborted), got {rc}"
    assert processed == ["a.pdf"], (
        f"subprocess path must abort after detecting cuda_poisoned in "
        f"debug.log; got {processed}")


def test_main_cpu_fallback_recovers_subprocess_cuda_poisoned(
        monkeypatch, tmp_path):
    """Default subprocess path: on cuda_poisoned in debug.log, engage CPU
    fallback, retry the same PDF (subprocess inherits --cpu), then continue
    with subsequent PDFs in CPU mode."""
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    a = input_dir / "a.pdf"
    b = input_dir / "b.pdf"
    a.write_bytes(b"%PDF-1.4\n%%EOF\n")
    b.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"

    monkeypatch.setattr(cli.env, "check", lambda venv, auto_yes: None)
    monkeypatch.setattr(cli, "_gpu_health_probe", lambda: None)

    calls: list[tuple[str, bool]] = []

    def fake_subprocess(pdf, out_dir, args, input_dir_override=None):
        calls.append((pdf.name, args.cpu))
        stem_dir = out_dir / pdf.stem
        stem_dir.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1:
            (stem_dir / "debug.log").write_text(
                "[2026-04-30 12:00:00] === done ===\n"
                "[2026-04-30 12:00:00] status=failed "
                "reason=cuda_poisoned: GPU CUDA context poisoned\n",
                encoding="utf-8")
            return (False, "", 0.0, 0.0)
        (stem_dir / f"{pdf.stem}.md").write_text("# ok\n", encoding="utf-8")
        (stem_dir / "manifest.json").write_text(
            '{"engine":"marker","quality_score":1.0}', encoding="utf-8")
        return (True, "marker", 1.0, 0.1)

    monkeypatch.setattr(cli, "_process_one_subprocess", fake_subprocess)

    monkeypatch.setattr(
        "sys.argv",
        ["alchemd",
         "--input-dir", str(input_dir),
         "--output-dir", str(out),
         "--no-auto-slice",
         "-y"])

    rc = cli.main()
    assert rc == 0, f"expected exit 0 after CPU recovery, got {rc}"
    assert calls == [
        ("a.pdf", False),
        ("a.pdf", True),
        ("b.pdf", True),
    ], f"unexpected call sequence: {calls}"


def test_find_existing_output_detects_newer_md(tmp_path):
    pdf = tmp_path / "foo.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    out = tmp_path / "out"
    stem_dir = out / "foo"
    stem_dir.mkdir(parents=True)
    md = stem_dir / "foo.md"
    md.write_text("ok")
    import os, time
    os.utime(md, (time.time() + 10, time.time() + 10))

    assert cli.find_existing_output(pdf, out) == md
