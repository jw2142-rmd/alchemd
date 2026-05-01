"""MinerU engine. Shells out to the `mineru` CLI — the Python API surface is unstable across versions.

CLI reference: `mineru -p <pdf> -o <out_dir> --method auto` produces <stem>.md + images/.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from alchemd.engines.base import EngineError, EngineResult


class MinerUEngine:
    name = "mineru"

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or shutil.which("mineru")

    def convert(self, pdf: Path, out_dir: Path) -> EngineResult:
        t0 = time.time()
        if not self._binary:
            raise EngineError(self.name, "resolve",
                              "mineru CLI not found on PATH — `pip install "
                              "mineru` should expose the console script. "
                              "If installed via a different venv, activate "
                              "that venv before running.")

        work = out_dir / "mineru_work"
        work.mkdir(parents=True, exist_ok=True)
        # --method auto: mineru uses the text layer when present and OCR only
        # when needed. Forcing 'ocr' on a 449-page text-layer PDF was the
        # cause of a 3.16 GiB CUDA allocation request / OOM on a 16 GiB GPU.
        cmd = [self._binary, "-p", str(pdf), "-o", str(work), "--method", "auto"]

        # expandable_segments avoids fragmentation OOMs where plenty of GPU
        # memory is free but no single contiguous block is large enough.
        env = os.environ.copy()
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600, env=env)
        except subprocess.CalledProcessError as exc:
            stderr_full = (exc.stderr or b"").decode("utf-8", errors="replace")
            stdout_full = (exc.stdout or b"").decode("utf-8", errors="replace")
            # mineru emits its actual error near the end of stderr; keep the tail.
            stderr = stderr_full[-3000:] if len(stderr_full) > 3000 else stderr_full
            stdout_tail = stdout_full[-500:] if len(stdout_full) > 500 else stdout_full
            raise EngineError(self.name, "cli",
                              f"exit={exc.returncode}\n--stderr--\n{stderr}\n"
                              f"--stdout tail--\n{stdout_tail}")
        except subprocess.TimeoutExpired:
            raise EngineError(self.name, "cli",
                              "mineru timeout 3600s — likely causes: very "
                              "large PDF (consider auto-slice), forced OCR "
                              "on a text-layer PDF, or GPU stuck. Check "
                              "nvidia-smi and reboot if utilization is 0% "
                              "with allocated memory.")

        md_files = list(work.rglob("*.md"))
        if not md_files:
            raise EngineError(self.name, "output", "no .md produced")
        markdown = md_files[0].read_text(encoding="utf-8", errors="replace")

        images_dir = out_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        images: list[Path] = []
        for img in work.rglob("*.png"):
            dst = images_dir / img.name
            try:
                shutil.move(str(img), str(dst))
                images.append(dst)
            except Exception:
                continue

        return EngineResult(markdown=markdown, images=images,
                            engine=self.name, elapsed=time.time() - t0)
