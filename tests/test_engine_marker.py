from pathlib import Path

from alchemd.engines import marker


def test_marker_engine_name():
    eng = marker.MarkerEngine()
    assert eng.name == "marker"


def test_marker_converts_clean_pdf(clean_pdf, tmp_output):
    eng = marker.MarkerEngine()
    result = eng.convert(clean_pdf, tmp_output)
    assert result.engine == "marker"
    assert len(result.markdown.strip()) > 0
    assert result.elapsed > 0
