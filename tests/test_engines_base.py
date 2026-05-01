from pathlib import Path

from alchemd.engines import base


def test_engine_result_has_fields():
    r = base.EngineResult(
        markdown="# x", images=[Path("a.png")], engine="marker", elapsed=1.5,
    )
    assert r.markdown == "# x"
    assert r.engine == "marker"
    assert r.elapsed == 1.5
    assert r.images == [Path("a.png")]


def test_engine_error_carries_stage_and_cause():
    cause = RuntimeError("boom")
    e = base.EngineError("marker", "convert", cause)
    assert e.engine == "marker"
    assert e.stage == "convert"
    assert e.__cause__ is None  # raise-from sets __cause__, dataclass doesn't
    assert "boom" in str(e)
