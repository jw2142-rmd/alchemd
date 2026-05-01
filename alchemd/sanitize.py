"""Normalize any PDF into an engine-compatible copy. Always sanitizes — no skip path.

Tier 1: Ghostscript pdfwrite (text-layer preserved)
Tier 2: Ghostscript rasterize via -sDEVICE=pdfimage24 (one-shot rasterized PDF).
        Never touches pypdfium2 on the source, so it works for originals that
        pypdfium2 cannot parse (common cause of total-pipeline failure).

fitz / PyMuPDF is NOT used anywhere.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from alchemd import env, ui


@dataclass
class SanitizeResult:
    ok: bool
    path: Path
    is_temp: bool
    tier: str            # "ghostscript" | "rasterize" | ""
    elapsed: float
    events: list[str] = field(default_factory=list)  # human-readable log lines


VERIFY_SAMPLE_COUNT = 10


def _sample_indices(n: int, k: int = VERIFY_SAMPLE_COUNT) -> list[int]:
    """k page indices evenly spread across [0, n-1]. Always includes 0 and n-1."""
    if n <= 0:
        return []
    if n <= k:
        return list(range(n))
    step = (n - 1) / (k - 1)
    return sorted({int(round(i * step)) for i in range(k)})


def _verify(pdf: Path) -> tuple[bool, str]:
    """Load check + render a spread of pages. Returns (ok, reason).

    Rendering just page 0 is too lax — GS can emit output whose page 0 loads
    fine but later pages fail, which engines then hit mid-run. Sample
    VERIFY_SAMPLE_COUNT pages evenly across the doc so page-level damage is
    caught before the engine."""
    try:
        import pypdfium2 as pdfium
    except Exception as exc:
        return False, f"pypdfium2 import: {type(exc).__name__}: {exc}"
    try:
        d = pdfium.PdfDocument(str(pdf))
    except Exception as exc:
        return False, f"load: {type(exc).__name__}: {exc}"
    try:
        n = len(d)
        if n <= 0:
            return False, "zero pages"
        indices = _sample_indices(n)
        for i in indices:
            try:
                page = d[i]
            except Exception as exc:
                return False, f"open page {i}: {type(exc).__name__}: {exc}"
            try:
                bitmap = page.render(scale=0.25)
                bitmap.close()
            except Exception as exc:
                return False, f"render page {i}: {type(exc).__name__}: {exc}"
            finally:
                page.close()
        return True, f"{n} pages, sampled {indices}"
    finally:
        d.close()


_TIMEOUT_CAP_SEC = 4 * 3600  # absolute watchdog ceiling


def _scaled_timeout(src: Path, base_sec: int, per_mb_sec: int) -> int:
    """Scale a subprocess timeout with input size. Speed is not a constraint
    per spec — we want overnight-safe runs on large books, capped at 4h so
    a genuinely-stuck process eventually surfaces."""
    try:
        size_mb = max(1, src.stat().st_size // (1024 * 1024))
    except OSError:
        size_mb = 1
    return min(_TIMEOUT_CAP_SEC, max(base_sec, per_mb_sec * size_mb))


def ghostscript(src: Path, dst: Path, timeout: int | None = None) -> None:
    gs = env.find_ghostscript()
    if not gs:
        raise RuntimeError(
            "ghostscript not found on PATH — install from "
            "https://www.ghostscript.com/releases/gsdnld.html "
            "(or `winget install ArtifexSoftware.GhostScript` on Windows / "
            "`brew install ghostscript` on macOS / "
            "`apt-get install ghostscript` on Debian/Ubuntu)")
    if timeout is None:
        timeout = _scaled_timeout(src, base_sec=600, per_mb_sec=60)
    cmd = [
        gs,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        "-dPrinted=false",
        "-dColorConversionStrategy=/LeaveColorUnchanged",
        "-dDownsampleColorImages=false",
        "-dDownsampleGrayImages=false",
        "-dDownsampleMonoImages=false",
        f"-sOutputFile={dst}",
        str(src),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)


def rasterize(src: Path, dst: Path, dpi: int = 150, timeout: int | None = None) -> None:
    """Render the whole document to a rasterized PDF in one Ghostscript pass
    via -sDEVICE=pdfimage24. Each page becomes an embedded color image; no
    intermediate PNGs, no img2pdf step.

    The previous PNG-then-img2pdf path produced output that pypdfium2 refused
    to load on page 0 even when the PNGs themselves were fine — observed on
    a_non_random_walk (449 pages) and the_master_swing_trader (377 pages).
    pdfimage24 writes a structurally-clean PDF that pypdfium2 reads without
    issue."""
    gs = env.find_ghostscript()
    if not gs:
        raise RuntimeError(
            "ghostscript not found on PATH — install from "
            "https://www.ghostscript.com/releases/gsdnld.html "
            "(or `winget install ArtifexSoftware.GhostScript` on Windows / "
            "`brew install ghostscript` on macOS / "
            "`apt-get install ghostscript` on Debian/Ubuntu)")

    if timeout is None:
        timeout = _scaled_timeout(src, base_sec=1800, per_mb_sec=120)

    cmd = [
        gs,
        "-sDEVICE=pdfimage24",
        f"-r{dpi}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        f"-sOutputFile={dst}",
        str(src),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)


def _mktmp() -> Path:
    t = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    t.close()
    return Path(t.name)


def run(src: Path, force_rasterize: bool = False,
        on_event: Callable[[str], None] | None = None) -> SanitizeResult:
    """Always sanitize. Try Ghostscript, fall back to rasterize. Never return
    the original. With force_rasterize, skip tier 1 — used when an engine
    downstream hit 'Failed to load page' on the tier-1 output, meaning GS
    pdfwrite produced page-level damage our verify sample missed.

    on_event receives each event string as it happens. Use for live-streaming
    progress into a write-through log — events collected into the returned
    list are otherwise lost if the process is killed mid-sanitize (observed:
    empty debug.log after '=== sanitize ===' on the_master_swing_trader)."""
    t0 = time.time()
    events: list[str] = []

    def emit(msg: str) -> None:
        events.append(msg)
        if on_event is not None:
            on_event(msg)

    if force_rasterize:
        emit("forced: skipping ghostscript, going direct to rasterize")
    else:
        # Tier 1: Ghostscript
        tmp = _mktmp()
        gs_timeout = _scaled_timeout(src, base_sec=600, per_mb_sec=60)
        try:
            emit(f"ghostscript: start src={src.name} timeout={gs_timeout}s")
            ghostscript(src, tmp, timeout=gs_timeout)
            ok, reason = _verify(tmp)
            if ok:
                emit(f"ghostscript: loadable ({reason})")
                return SanitizeResult(ok=True, path=tmp, is_temp=True,
                                      tier="ghostscript", elapsed=time.time() - t0,
                                      events=events)
            emit(f"ghostscript: verify failed ({reason})")
        except subprocess.TimeoutExpired:
            emit(f"ghostscript: timeout {gs_timeout}s")
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[:200]
            emit(f"ghostscript: exit={exc.returncode} stderr={stderr}")
        except Exception as exc:
            emit(f"ghostscript: error {type(exc).__name__}: {exc}")
        tmp.unlink(missing_ok=True)

    # Tier 2: Rasterize
    tmp = _mktmp()
    ras_timeout = _scaled_timeout(src, base_sec=1800, per_mb_sec=120)
    try:
        emit(f"rasterize: start (150 DPI, timeout={ras_timeout}s)")
        rasterize(src, tmp, dpi=150, timeout=ras_timeout)
        ok, reason = _verify(tmp)
        if ok:
            emit(f"rasterize: loadable ({reason})")
            return SanitizeResult(ok=True, path=tmp, is_temp=True,
                                  tier="rasterize", elapsed=time.time() - t0,
                                  events=events)
        emit(f"rasterize: verify failed ({reason})")
    except subprocess.TimeoutExpired:
        emit(f"rasterize: timeout {ras_timeout}s")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[:300]
        emit(f"rasterize: exit={exc.returncode} stderr={stderr}")
    except Exception as exc:
        emit(f"rasterize: error {type(exc).__name__}: {exc}")
    tmp.unlink(missing_ok=True)

    return SanitizeResult(ok=False, path=src, is_temp=False,
                          tier="", elapsed=time.time() - t0, events=events)
