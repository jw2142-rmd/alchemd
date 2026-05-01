"""Engine wrapper that runs the wrapped engine in a fresh subprocess.

Isolates model-loading memory between engine attempts. Marker's ~3 GB
of surya/CUDA state releases when its subprocess exits, BEFORE docling's
subprocess loads its own models. Without this, failed engines stack in
RAM and the parent python can easily push 10+ GB resident on a large
book — observed thrashing into swap on a 449-page book.

The wrapper keeps the protocol identical to in-process engines: name +
convert(pdf, out_dir) -> EngineResult. Engine-specific behavior (chunking,
retries inside the engine) still lives in the engine module itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from alchemd.engines.base import EngineError, EngineResult

DEFAULT_TIMEOUT_SEC = 4 * 3600  # outer cap; adaptive per-convert is preferred

# Page-aware adaptive timeout knobs. Empirical from the 2026-04-28 / 04-29
# runs: marker on small papers averages ~5-15 s/page when healthy; 30 s/page
# is comfortable headroom (covers slow tablets-of-figures pages) without
# letting a 50p paper hang for 4 hours like the 2026-04-29 incident on
# PDF 17 (where marker timed out at 14400 s on a 50p paper because the
# CUDA driver was poisoned). 60 s baseline absorbs model load.
_PER_PAGE_TIMEOUT_SEC = 30
_BASELINE_TIMEOUT_SEC = 60

# F21: GPU-tuned per-page rate is 5-10x too tight on CPU (S8 stress: marker
# on a 1p paper hit the 90s timeout under CUDA_VISIBLE_DEVICES=-1). Detect
# CPU-only torch once at module load and apply a multiplier so the same
# pipeline works on CPU hosts without per-PDF tuning.
_CPU_TIMEOUT_MULTIPLIER = 6


def _detect_cuda_available() -> bool:
    """Probe torch.cuda once at module import. False if torch isn't installed,
    if there's no CUDA build, or if no device is enumerated. Cached because
    the probe is non-trivial and we call adaptive_timeout per-PDF."""
    try:
        import torch  # noqa: F401 — heavy import, intentional one-shot
        return bool(torch.cuda.is_available())
    except Exception:
        return False


_cuda_available_cached = _detect_cuda_available()


def adaptive_timeout(page_count: int | None, cap: int = DEFAULT_TIMEOUT_SEC) -> int:
    """Compute a per-convert timeout: baseline + per-page * pages, capped.

    page_count=None falls back to the cap (legacy behaviour). The cap stays
    high enough to cover a 1500-page slice run under marker; the linear
    formula bounds damage on small books when something hangs.

    F21: when CUDA is not available, multiply by _CPU_TIMEOUT_MULTIPLIER so
    CPU-only hosts don't false-timeout marker on small documents.
    """
    if page_count is None or page_count <= 0:
        return cap
    base = _BASELINE_TIMEOUT_SEC + _PER_PAGE_TIMEOUT_SEC * page_count
    if not _cuda_available_cached:
        base *= _CPU_TIMEOUT_MULTIPLIER
    return min(base, cap)

# Substrings in subprocess stderr/stdout/error-payload that indicate the failure
# was caused by host-level memory pressure (RAM, pagefile), not a defect in the
# engine or input PDF. When detected we surface an actionable reason instead
# of dumping the raw OS error, so the operator knows to close other RAM users
# or reboot rather than hunting for a code bug.
_MEMORY_PRESSURE_MARKERS = (
    "openblas error: memory allocation",     # numpy/torch allocator giving up
    "winerror 1455",                         # Windows: paging file too small
    "the paging file is too small",
    "cannot allocate memory",                # Linux RLIMIT/oom
    "process pool was terminated abruptly",  # ProcessPoolExecutor child OOM-killed
)

# Substrings that indicate the GPU's CUDA driver state is poisoned. Two paths
# into this state, observed:
#   1. Force-killing a torch process holding a live CUDA context.
#   2. A docling/marker subprocess hitting CUDA OOM mid-conversion (the
#      allocator gives up, then every subsequent kernel launch on the same
#      device returns "invalid resource handle" — driver in dead state).
# Once any subprocess produces these markers, every following CUDA op on the
# host is suspect: marker fallbacks hang the full timeout, next-PDF attempts
# fail the same way. Reboot is the only fix. The driver short-circuits the
# engine chain on this class of failure and the cli aborts the whole batch.
_CUDA_POISONED_MARKERS = (
    "cuda error: out of memory",
    "cuda error: invalid resource handle",
    "cuda error: unknown error",
    "cudaerrorunknown",
    "cudaerroroutofmemory",
    "cudaerrorinvalidresourcehandle",
    "torch.cuda.outofmemoryerror",
)


def _matches_any(text: str, markers: tuple[str, ...]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(m in lowered for m in markers)


def _looks_like_memory_pressure(text: str) -> bool:
    return _matches_any(text, _MEMORY_PRESSURE_MARKERS)


def _looks_like_cuda_poisoning(text: str) -> bool:
    return _matches_any(text, _CUDA_POISONED_MARKERS)


# Single env var applied to every engine subprocess. Documented mitigation
# for "plenty of GPU memory free, but no contiguous block big enough"
# fragmentation OOMs — exactly the case that triggered the 2026-04-29
# CUDA poisoning event (50p PDF, 13.76 GiB free, fragmentation OOM trying
# to allocate 100 MiB). Previously only mineru.py set this; lifting it to
# the SubprocessEngine layer applies it uniformly.
_ENGINE_CUDA_ALLOC_CONF = "expandable_segments:True"


class SubprocessEngine:
    """Runs any engine_runner-supported engine in its own interpreter."""

    def __init__(self, name: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> None:
        self.name = name
        self._timeout = timeout

    def convert(self, pdf: Path, out_dir: Path,
                page_count: int | None = None) -> EngineResult:
        """Run the engine in a fresh subprocess. `page_count` (when supplied
        by the driver from the preflight profile) shortens the timeout for
        small PDFs, so a hung subprocess on a 50p paper bails in ~26 min
        instead of the 4-hour cap."""
        out_dir.mkdir(parents=True, exist_ok=True)
        fd, result_path_str = tempfile.mkstemp(
            suffix=".json", prefix=f"{self.name}_result_")
        os.close(fd)
        result_path = Path(result_path_str)

        timeout = adaptive_timeout(page_count, cap=self._timeout)

        try:
            cmd = [
                sys.executable, "-m", "alchemd.engine_runner",
                self.name, str(pdf), str(out_dir),
                "--result-path", str(result_path),
            ]
            # Apply the documented CUDA fragmentation-OOM mitigation to every
            # engine subprocess. setdefault so an env override from outside
            # still wins (some users set their own alloc config).
            child_env = os.environ.copy()
            child_env.setdefault("PYTORCH_CUDA_ALLOC_CONF", _ENGINE_CUDA_ALLOC_CONF)
            try:
                proc = subprocess.run(cmd, capture_output=True,
                                      timeout=timeout, env=child_env)
            except subprocess.TimeoutExpired:
                raise EngineError(
                    self.name, "subprocess",
                    f"engine subprocess timeout {timeout}s "
                    f"(page_count={page_count})")

            if not result_path.exists() or result_path.stat().st_size == 0:
                # Child died before writing payload — surface stdout AND stderr
                # tails so debug.log shows what killed it. Some crashes (pypdfium2
                # C++ aborts, "Fatal Python error" on Windows) write to stdout, not
                # stderr — capturing only stderr loses the actual cause.
                stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
                stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
                stderr_tail = stderr[-3000:] if len(stderr) > 3000 else stderr
                stdout_tail = stdout[-3000:] if len(stdout) > 3000 else stdout
                # CUDA poisoning is checked FIRST — a CUDA error in stderr also
                # tends to trip OpenBLAS/allocator markers downstream, but the
                # actionable advice (reboot) is different from generic memory
                # pressure (close other users).
                if _looks_like_cuda_poisoning(stderr) or _looks_like_cuda_poisoning(stdout):
                    raise EngineError(
                        self.name, "cuda_poisoned",
                        "GPU CUDA context poisoned (driver in dead state) — "
                        "reboot required before retrying any GPU engine. "
                        f"exit={proc.returncode} "
                        f"stderr_tail={stderr_tail[-500:]} "
                        f"stdout_tail={stdout_tail[-500:]}")
                if _looks_like_memory_pressure(stderr) or _looks_like_memory_pressure(stdout):
                    raise EngineError(
                        self.name, "memory_pressure",
                        "host out of memory (RAM/pagefile exhausted) — "
                        "close other RAM users or reboot, then retry. "
                        f"exit={proc.returncode} "
                        f"stderr_tail={stderr_tail[-500:]} "
                        f"stdout_tail={stdout_tail[-500:]}")
                raise EngineError(
                    self.name, "subprocess",
                    f"no payload written; exit={proc.returncode} "
                    f"stdout_tail={stdout_tail} stderr_tail={stderr_tail}")

            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                # Engine wrote a structured failure payload (e.g. docling's
                # exporter raising on CUDA OOM). Re-classify CUDA-poisoning
                # signatures here too — without this check, a docling OOM
                # comes back as a regular EngineError(stage="convert") and
                # the driver retries marker on a poisoned GPU, hanging until
                # the per-engine timeout. (Root cause of the 2026-04-29
                # 4-hour-marker-hang on PDF 17.)
                err_text = str(payload.get("error", ""))
                tb_text = str(payload.get("traceback", ""))
                if _looks_like_cuda_poisoning(err_text) or _looks_like_cuda_poisoning(tb_text):
                    raise EngineError(
                        self.name, "cuda_poisoned",
                        "GPU CUDA context poisoned (driver in dead state) — "
                        "reboot required before retrying any GPU engine. "
                        f"engine_error={err_text[:1000]}")
                raise EngineError(
                    self.name, payload.get("stage", "convert"),
                    payload.get("error", "unknown"))

            md_path = Path(payload["markdown_path"])
            try:
                markdown = md_path.read_text(encoding="utf-8")
            finally:
                md_path.unlink(missing_ok=True)
            images = [Path(p) for p in payload.get("images", [])]
            return EngineResult(
                markdown=markdown, images=images, engine=self.name,
                elapsed=float(payload.get("elapsed", 0.0)),
                notes=list(payload.get("notes", [])))
        finally:
            result_path.unlink(missing_ok=True)
