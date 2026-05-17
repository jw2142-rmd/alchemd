"""Docling engine (IBM). Strong on tables + figures."""
from __future__ import annotations

import time
from pathlib import Path

from alchemd.engines.base import EngineError, EngineResult

# Docling is pinned to CPU. Even with batch sizes of 1 (down from defaults
# 4/4/4) docling continued to fragment VRAM on heavy art / large books and
# poison the driver — Witchlight (2026-05-17) and the 2026-04-29 incident
# were both docling-side `CUDA error: unknown / out of memory` despite
# >13 GiB free. Running docling on CPU eliminates the entire fragmentation
# class. Marker keeps GPU. CPU docling is slower (~3-5x on table-heavy
# books) but never poisons the driver, and the adaptive subprocess
# timeout already auto-scales 6x when CUDA_VISIBLE_DEVICES=-1.
DOCLING_LAYOUT_BATCH = 1
DOCLING_OCR_BATCH = 1
DOCLING_TABLE_BATCH = 1
DOCLING_NUM_THREADS = 1


class DoclingEngine:
    name = "docling"

    def __init__(self) -> None:
        self._converter = None

    def _ensure(self):
        if self._converter is None:
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.datamodel.accelerator_options import (
                AcceleratorDevice,
                AcceleratorOptions,
            )

            opts = PdfPipelineOptions(
                accelerator_options=AcceleratorOptions(
                    num_threads=DOCLING_NUM_THREADS,
                    device=AcceleratorDevice.CPU,
                ),
                ocr_batch_size=DOCLING_OCR_BATCH,
                layout_batch_size=DOCLING_LAYOUT_BATCH,
                table_batch_size=DOCLING_TABLE_BATCH,
            )
            self._converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
            )
        return self._converter

    def convert(self, pdf: Path, out_dir: Path) -> EngineResult:
        t0 = time.time()
        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        try:
            converter = self._ensure()
            result = converter.convert(str(pdf))
            doc = result.document
            markdown = doc.export_to_markdown()
        except Exception as exc:
            raise EngineError(self.name, "convert",
                              f"{type(exc).__name__}: {exc}")

        images: list[Path] = []
        try:
            for i, pic in enumerate(getattr(doc, "pictures", []) or []):
                img = getattr(pic, "image", None) or getattr(pic, "pil_image", None)
                if img is None:
                    continue
                path = images_dir / f"{pdf.stem}_p{getattr(pic, 'page_no', 0)}_{i}.png"
                img.save(str(path))
                images.append(path)
        except Exception as exc:
            # Image export is best-effort; do not fail the whole conversion
            return EngineResult(markdown=markdown, images=images,
                                engine=self.name, elapsed=time.time() - t0,
                                notes=[f"image export skipped: {exc}"])

        return EngineResult(markdown=markdown, images=images,
                            engine=self.name, elapsed=time.time() - t0)
