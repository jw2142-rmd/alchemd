"""Marker engine. Lazy-loads models on first call. Chunks >300-page PDFs, 2 parallel workers."""
from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from alchemd.engines.base import EngineError, EngineResult

PAGE_CHUNK_THRESHOLD = 300
PAGE_CHUNK_SIZE = 200
# Single worker: marker's models dict is not documented thread-safe. Two threads
# running PdfConverter on shared CUDA state corrupted the heap on Windows
# (STATUS_STACK_BUFFER_OVERRUN / 0xC0000409 fastfail) on the 449p book. Speed is
# not a constraint per spec.
CHUNK_WORKERS = 1

MARKER_CONFIG = {
    "layout_batch_size": 16,
    "detection_batch_size": 16,
    "recognition_batch_size": 16,
    "lowres_image_dpi": 72,
    "highres_image_dpi": 144,
}


class MarkerEngine:
    name = "marker"

    def __init__(self) -> None:
        self._models = None
        self._lock = Lock()

    def _ensure_models(self):
        with self._lock:
            if self._models is None:
                from marker.models import create_model_dict
                self._models = create_model_dict()
        return self._models

    def _page_count(self, pdf: Path) -> int:
        import pypdfium2 as pdfium
        d = pdfium.PdfDocument(str(pdf))
        try:
            return len(d)
        finally:
            d.close()

    def _convert_range(self, pdf: Path, page_range: list[int] | None,
                       images_dir: Path) -> tuple[str, list[Path]]:
        from marker.converters.pdf import PdfConverter
        cfg = dict(MARKER_CONFIG)
        if page_range is not None:
            cfg["page_range"] = page_range
        result = PdfConverter(artifact_dict=self._ensure_models(), config=cfg)(str(pdf))
        images: list[Path] = []
        # Marker returns result.images as dict[name, PIL.Image]
        raw_images = getattr(result, "images", None) or {}
        for name, img in raw_images.items():
            path = images_dir / Path(name).name
            img.save(str(path))
            images.append(path)
        return result.markdown, images

    def convert(self, pdf: Path, out_dir: Path) -> EngineResult:
        t0 = time.time()
        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        try:
            page_count = self._page_count(pdf)
        except Exception as exc:
            raise EngineError(self.name, "page_count",
                              f"{type(exc).__name__}: {exc}")

        try:
            if page_count > PAGE_CHUNK_THRESHOLD:
                ranges = [
                    list(range(s, min(s + PAGE_CHUNK_SIZE, page_count)))
                    for s in range(0, page_count, PAGE_CHUNK_SIZE)
                ]
                parts: list[str | None] = [None] * len(ranges)
                all_imgs: list[Path] = []
                with ThreadPoolExecutor(max_workers=CHUNK_WORKERS) as pool:
                    futs = {pool.submit(self._convert_range, pdf, r, images_dir): i
                            for i, r in enumerate(ranges)}
                    for fut in as_completed(futs):
                        i = futs[fut]
                        md, imgs = fut.result()
                        parts[i] = md
                        all_imgs.extend(imgs)
                markdown = "\n\n".join(p for p in parts if p)
                return EngineResult(markdown=markdown, images=all_imgs,
                                    engine=self.name, elapsed=time.time() - t0)
            md, imgs = self._convert_range(pdf, None, images_dir)
            return EngineResult(markdown=md, images=imgs,
                                engine=self.name, elapsed=time.time() - t0)
        except Exception as exc:
            raise EngineError(self.name, "convert",
                              f"{type(exc).__name__}: {exc}")
