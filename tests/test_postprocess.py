from pathlib import Path

from alchemd import postprocess


def test_normalize_absolute_image_paths(tmp_path):
    md = f"![x]({tmp_path / 'images' / 'a.png'})"
    out = postprocess.clean(md, images_dir=tmp_path / "images", notes=[])
    assert "./images/a.png" in out


def test_normalize_bare_filename_image_paths(tmp_path):
    md = "![](_page_0_Picture_1.jpeg)"
    out = postprocess.clean(md, images_dir=tmp_path / "images", notes=[])
    assert "./images/_page_0_Picture_1.jpeg" in out


def test_leave_external_and_already_prefixed_image_paths(tmp_path):
    md = (
        "![a](https://example.com/x.png)\n"
        "![b](./images/y.png)\n"
        "![c](sub/z.png)\n"
    )
    out = postprocess.clean(md, images_dir=tmp_path / "images", notes=[])
    assert "https://example.com/x.png" in out
    assert "./images/y.png" in out
    assert "sub/z.png" in out


def test_collapse_triple_blank_lines():
    md = "a\n\n\n\n\nb"
    out = postprocess.clean(md, images_dir=Path("."), notes=[])
    assert "\n\n\n" not in out


def test_fix_hyphen_line_break():
    md = "word-\nword"
    out = postprocess.clean(md, images_dir=Path("."), notes=[])
    assert "wordword" in out


def test_drop_malformed_table_row_and_note_it():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n| only-one |\n| 3 | 4 |"
    notes: list[str] = []
    out = postprocess.clean(md, images_dir=Path("."), notes=notes)
    assert "only-one" not in out
    assert any("dropped" in n.lower() for n in notes)
