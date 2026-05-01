from alchemd import chunking


MD = """# Title

Intro text.

## Section A

Alpha content.

### Sub A1

Detail.

## Section B

#### Deep sub — should not split

Still in B.
"""


def test_chunk_splits_on_h1_h2_h3_only():
    chunks = chunking.chunk(MD, source="x.pdf", engine="marker", quality_score=0.95)
    headings = [c["heading"] for c in chunks]
    assert headings == ["Title", "Section A", "Sub A1", "Section B"]


def test_chunk_preserves_full_section_body():
    chunks = chunking.chunk(MD, source="x.pdf", engine="marker", quality_score=0.9)
    b = next(c for c in chunks if c["heading"] == "Section B")
    assert "Deep sub" in b["text"]


def test_chunk_records_provenance_fields():
    chunks = chunking.chunk("# H\n\ntext", source="p.pdf",
                            engine="docling", quality_score=0.8)
    assert chunks[0]["source"] == "p.pdf"
    assert chunks[0]["engine"] == "docling"
    assert chunks[0]["quality_score"] == 0.8
    assert chunks[0]["section_index"] == 0


def test_chunk_empty_input_returns_empty_list():
    assert chunking.chunk("", source="e.pdf", engine="marker", quality_score=1.0) == []


def test_chunk_leading_content_without_heading_becomes_intro():
    md = "Some prose with no heading at top.\n\n## First"
    chunks = chunking.chunk(md, source="x.pdf", engine="marker", quality_score=1.0)
    assert chunks[0]["heading"] == "Intro"
