from alchemd import router
from alchemd.preflight import PdfProfile


def _prof(**kw) -> PdfProfile:
    base = dict(page_count=50, is_encrypted=False, has_text_layer=0.9,
                table_density=0.0, is_scanned=False, max_table_cells=0)
    base.update(kw)
    return PdfProfile(**base)


def test_scanned_routes_mineru_first():
    order = router.decide(_prof(is_scanned=True, has_text_layer=0.01))
    assert order[0] == "mineru"


def test_table_heavy_routes_docling_first():
    order = router.decide(_prof(table_density=3.0))
    assert order[0] == "docling"


def test_default_routes_marker_first():
    order = router.decide(_prof())
    assert order[0] == "marker"


def test_router_returns_all_three_engines_in_order():
    order = router.decide(_prof())
    assert set(order) == {"marker", "docling", "mineru"}
    assert len(order) == 3


def test_custom_config_changes_threshold():
    # Bump every threshold so 3.0 density on a 50-page doc is below all of them.
    cfg = router.RouterConfig(
        table_density_threshold=10.0,
        est_total_tables_threshold=1000.0,
        max_table_cells_threshold=10_000,
    )
    order = router.decide(_prof(table_density=3.0), cfg=cfg)
    assert order[0] == "marker"


def test_long_book_with_sparse_tables_routes_docling():
    # 449 pages * 0.375 tables/page = ~168 tables — docling-worthy by volume
    # even though density alone is below the per-page threshold.
    order = router.decide(_prof(page_count=449, table_density=0.375))
    assert order[0] == "docling"


def test_single_large_table_routes_docling():
    # A short PDF with one big table (e.g. a financial summary) — biggest
    # tables matter most, route to docling regardless of density.
    order = router.decide(_prof(page_count=10, table_density=0.1, max_table_cells=120))
    assert order[0] == "docling"


def test_short_doc_with_one_small_table_routes_marker():
    # 5-page PDF with one small table is fine for marker.
    order = router.decide(_prof(page_count=5, table_density=0.2, max_table_cells=12))
    assert order[0] == "marker"
