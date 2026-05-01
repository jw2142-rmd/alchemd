"""engine_runner is the subprocess-side entry point for SubprocessEngine.
Exercise it directly (not via subprocess) so failures surface fast."""
import json
from pathlib import Path

import pytest

from alchemd import engine_runner
from alchemd.engines.base import EngineError, EngineResult


class _StubEngine:
    name = "stub"

    def __init__(self, md: str = "# ok\n\nbody", fail: Exception | None = None):
        self._md = md
        self._fail = fail

    def convert(self, pdf: Path, out_dir: Path) -> EngineResult:
        if self._fail is not None:
            raise self._fail
        return EngineResult(markdown=self._md, images=[], engine=self.name,
                            elapsed=0.25, notes=["ok"])


def _run(monkeypatch, tmp_path, engine, name="marker"):
    monkeypatch.setattr(engine_runner, "_load_engine", lambda n: engine)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result_path = tmp_path / "result.json"
    out_dir = tmp_path / "out"
    rc = engine_runner.run(name, pdf, out_dir, result_path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    return rc, payload


def test_runner_writes_success_payload_and_markdown_file(tmp_path, monkeypatch):
    rc, payload = _run(monkeypatch, tmp_path, _StubEngine(md="# hello"))
    assert rc == 0
    assert payload["ok"] is True
    assert payload["engine"] == "stub"
    assert payload["elapsed"] == 0.25
    assert payload["notes"] == ["ok"]
    md = Path(payload["markdown_path"]).read_text(encoding="utf-8")
    assert md == "# hello"


def test_runner_writes_failure_payload_with_engineerror_stage(tmp_path, monkeypatch):
    err = EngineError("stub", "convert", "forced")
    rc, payload = _run(monkeypatch, tmp_path, _StubEngine(fail=err))
    assert rc == 2
    assert payload["ok"] is False
    assert payload["error_type"] == "EngineError"
    assert payload["stage"] == "convert"
    assert "forced" in payload["error"]
    assert "Traceback" in payload["traceback"]


def test_runner_writes_failure_payload_on_unexpected_exception(tmp_path, monkeypatch):
    rc, payload = _run(monkeypatch, tmp_path, _StubEngine(fail=RuntimeError("boom")))
    assert rc == 2
    assert payload["ok"] is False
    assert payload["error_type"] == "RuntimeError"
    assert payload["stage"] == "convert"
    assert "boom" in payload["error"]


def test_runner_rejects_unknown_engine_name(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result_path = tmp_path / "r.json"
    with pytest.raises(SystemExit):
        engine_runner.main([
            "totallymadeup", str(pdf), str(tmp_path / "out"),
            "--result-path", str(result_path)])
