"""Pick one engine per PDF; return the full fallback order."""
from __future__ import annotations

from dataclasses import dataclass

from alchemd.preflight import PdfProfile


@dataclass
class RouterConfig:
    # Route to docling when the document carries enough table workload that
    # marker's misses would compound, OR when any single table is large enough
    # that getting it right matters more than speed. Either condition trips it.
    table_density_threshold: float = 1.5      # mean tables/page (legacy signal)
    est_total_tables_threshold: float = 20.0  # extrapolated table count book-wide
    max_table_cells_threshold: int = 50       # any table this large → docling


_ALL = ("marker", "docling", "mineru")


def decide(profile: PdfProfile, cfg: RouterConfig | None = None) -> list[str]:
    cfg = cfg or RouterConfig()
    order, _reason = decide_with_reason(profile, cfg)
    return order


def decide_with_reason(profile: PdfProfile,
                       cfg: RouterConfig | None = None
                       ) -> tuple[list[str], str]:
    """Same as decide() but also returns a human-readable explanation of which
    threshold tripped. Lets debug.log explain *why* a route was picked, not
    just *what* (Phase-1 finding F16)."""
    cfg = cfg or RouterConfig()
    if profile.is_scanned:
        primary = "mineru"
        reason = (f"mineru: scanned (has_text_layer={profile.has_text_layer:.2f} "
                  f"< 0.10)")
    elif profile.max_table_cells >= cfg.max_table_cells_threshold:
        primary = "docling"
        reason = (f"docling: max_table_cells={profile.max_table_cells} "
                  f">= {cfg.max_table_cells_threshold}")
    elif profile.table_density >= cfg.table_density_threshold:
        primary = "docling"
        reason = (f"docling: table_density={profile.table_density:.2f} "
                  f">= {cfg.table_density_threshold}")
    elif profile.est_total_tables >= cfg.est_total_tables_threshold:
        primary = "docling"
        reason = (f"docling: est_total_tables={profile.est_total_tables:.1f} "
                  f">= {cfg.est_total_tables_threshold}")
    else:
        primary = "marker"
        reason = (f"marker: no docling/mineru triggers tripped "
                  f"(table_density={profile.table_density:.2f}, "
                  f"max_table_cells={profile.max_table_cells}, "
                  f"est_total_tables={profile.est_total_tables:.1f})")
    order = [primary] + [e for e in _ALL if e != primary]
    return order, reason
