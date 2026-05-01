from pathlib import Path

from alchemd import quality
from alchemd.preflight import PdfProfile


def _prof(**kw) -> PdfProfile:
    base = dict(page_count=10, is_encrypted=False, has_text_layer=0.9,
                table_density=0.0, is_scanned=False)
    base.update(kw)
    return PdfProfile(**base)


def test_short_output_fails():
    rep = quality.check("x" * 20, profile=_prof(page_count=10), images=[], images_dir=Path("."))
    assert rep.passed is False
    assert any("emptiness" in i.lower() for i in rep.issues)


def test_clean_long_output_passes():
    md = ("# Title\n\n" + "Lorem ipsum. " * 400)
    rep = quality.check(md, profile=_prof(page_count=2), images=[], images_dir=Path("."))
    assert rep.passed is True


def test_missing_image_file_fails(tmp_path):
    md = f"# t\n\n![x]({tmp_path / 'images' / 'ghost.png'})"
    rep = quality.check(md, profile=_prof(), images=[tmp_path / "images" / "ghost.png"],
                        images_dir=tmp_path / "images")
    assert rep.passed is False
    assert any("image missing" in i.lower() for i in rep.issues)


def test_table_density_high_but_no_table_warns():
    md = "# t\n\n" + "Lorem ipsum dolor sit amet. " * 400
    rep = quality.check(md, profile=_prof(table_density=5.0, page_count=2),
                        images=[], images_dir=Path("."))
    assert any("table" in i.lower() for i in rep.issues)
