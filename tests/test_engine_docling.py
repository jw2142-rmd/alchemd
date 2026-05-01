from alchemd.engines import docling as d


def test_docling_engine_name():
    assert d.DoclingEngine().name == "docling"


def test_docling_low_batch_constants():
    # Pinned to 1 to prevent VRAM fragmentation on table-heavy PDFs.
    # See 2026-04-29 CUDA poisoning incident.
    assert d.DOCLING_LAYOUT_BATCH == 1
    assert d.DOCLING_OCR_BATCH == 1
    assert d.DOCLING_TABLE_BATCH == 1
    assert d.DOCLING_NUM_THREADS == 1


def test_docling_converter_uses_low_batches():
    from docling.datamodel.base_models import InputFormat
    converter = d.DoclingEngine()._ensure()
    opts = converter.format_to_options[InputFormat.PDF].pipeline_options
    assert opts.layout_batch_size == 1
    assert opts.ocr_batch_size == 1
    assert opts.table_batch_size == 1
    assert opts.accelerator_options.num_threads == 1


def test_docling_converts_table_heavy_pdf(table_heavy_pdf, tmp_output):
    result = d.DoclingEngine().convert(table_heavy_pdf, tmp_output)
    assert result.engine == "docling"
    assert "|" in result.markdown or "<table" in result.markdown.lower()
