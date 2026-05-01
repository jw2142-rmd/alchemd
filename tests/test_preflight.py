from alchemd import preflight


def test_profile_dataclass_has_required_fields():
    prof = preflight.PdfProfile(
        page_count=10, is_encrypted=False, has_text_layer=0.9,
        table_density=0.1, is_scanned=False,
    )
    assert prof.page_count == 10
    assert prof.is_scanned is False


def test_probe_on_clean_pdf(clean_pdf):
    prof = preflight.probe(clean_pdf)
    assert prof.page_count > 0
    assert prof.is_encrypted is False
    assert prof.has_text_layer > 0.0
    assert prof.is_scanned is False


def test_probe_on_encrypted_pdf(encrypted_pdf):
    prof = preflight.probe(encrypted_pdf)
    assert prof.is_encrypted is True


def test_probe_on_scanned_pdf(scanned_pdf):
    prof = preflight.probe(scanned_pdf)
    assert prof.is_scanned is True
    assert prof.has_text_layer < 0.1


def test_probe_on_table_heavy_pdf(table_heavy_pdf):
    prof = preflight.probe(table_heavy_pdf)
    assert prof.table_density > 0.0
