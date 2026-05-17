"""Per-PDF orchestration: preflight → sanitize → route → convert → post → quality, with debug.log."""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from alchemd import postprocess, preflight, quality, router, sanitize, ui
from alchemd.engines.base import EngineError

# Substrings that indicate the engine hit page-level damage in the sanitized
# PDF (GS pdfwrite sometimes produces output where individual pages are
# unparseable). When ALL engines fail with one of these, retry the whole
# conversion on a rasterized sanitize output.
_PAGE_LOAD_MARKERS = ("failed to load page", "failed to load document",
                      "is not valid", "pdfiumerror")


def _is_page_load_failure(msg: str) -> bool:
    m = msg.lower()
    return any(marker in m for marker in _PAGE_LOAD_MARKERS)


@dataclass
class PdfResult:
    name: str
    ok: bool
    engine_used: str | None
    quality_score: float | None
    elapsed: float
    chunks: int = 0
    reason: str = ""


def _now() -> str:
    # UTC + ISO-8601 so cross-host correlation works (dev box vs homeserver).
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically: write a sibling .tmp then os.replace.
    Prevents a half-written file from being treated as a cache hit if the
    process dies mid-write (F4). On any failure (ENOSPC mid-write, replace
    error), the .tmp is unlinked so re-runs don't accumulate orphans (F24).
    Caller is responsible for catching OSError — see driver._try_engines
    for the disk-error classification (F23)."""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class _DebugLog:
    """Write-through log. Every write() is flushed to disk immediately so a
    hard crash (e.g. CUDA OOM killing the process) still leaves a breadcrumb
    for the last line reached."""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.write_text("", encoding="utf-8")
        self._fh = open(self.path, "a", encoding="utf-8")

    def _emit(self, line: str) -> None:
        self._fh.write(line + "\n")
        self._fh.flush()

    def write(self, line: str) -> None:
        self._emit(f"[{_now()}] {line}")

    def writelines(self, lines: list[str]) -> None:
        for line in lines:
            self.write(line)

    def section(self, title: str) -> None:
        self._emit("")
        self._emit(f"=== {title} ===")

    def flush(self) -> None:
        self._fh.flush()


def process_one(pdf: Path, output_dir: Path, engines: dict[str, object],
                router_config: router.RouterConfig | None = None,
                skip_sanitize: bool = False) -> PdfResult:
    """Convert one PDF. Always writes output/<stem>/debug.log, even on total failure."""
    t0 = time.time()
    stem = pdf.stem
    stem_dir = output_dir / stem
    stem_dir.mkdir(parents=True, exist_ok=True)
    log = _DebugLog(stem_dir / "debug.log")
    log.section(f"process_one: {pdf.name}")

    # Sanitize first — originals may be malformed (pypdfium2-incompatible),
    # so all downstream probing/conversion runs on the sanitized output.
    log.section("sanitize")
    if skip_sanitize:
        log.write("skipped by --no-sanitize")
        work_path = pdf
        is_temp = False
    else:
        sres = sanitize.run(pdf, on_event=log.write)
        log.write(f"tier={sres.tier} elapsed={sres.elapsed:.1f}s ok={sres.ok}")
        if not sres.ok:
            log.section("done")
            log.write("status=failed reason=sanitize: all tiers failed")
            log.flush()
            return PdfResult(pdf.name, False, None, None, time.time() - t0,
                             reason="sanitize: all tiers failed")
        work_path = sres.path
        is_temp = sres.is_temp

    # Preflight runs on the sanitized file so the profile is reliable, but
    # we ALSO pass `original_pdf=pdf` so the probe checks encryption on the
    # pre-sanitize original — ghostscript transparently strips user-password
    # protection (S4: sanitize succeeds → page is empty → engine produces 0
    # chars → quality emptiness fail with no encryption signal). With the
    # original-encryption flag we can short-circuit with a useful reason.
    log.section("preflight")
    try:
        profile = preflight.probe(work_path, original_pdf=pdf)
        log.write(f"profile: {asdict(profile)}")
    except Exception as exc:
        log.write(f"preflight failed: {exc}")
        log.write(traceback.format_exc())
        log.section("done")
        log.write(f"status=failed reason=preflight: {exc}")
        log.flush()
        if is_temp and work_path != pdf:
            try:
                work_path.unlink(missing_ok=True)
            except OSError:
                pass
        return PdfResult(pdf.name, False, None, None, time.time() - t0,
                         reason=f"preflight: {exc}")

    # F20: encrypted-PDF fast-fail. Sanitize stripped the user-password but
    # the content is gone — running engines would just produce empty md and
    # surface as misleading "emptiness" failures. Bail with a reason the
    # operator can act on (re-run with --password once that flag is wired
    # through sanitize and pikepdf).
    if profile.is_originally_encrypted:
        log.section("done")
        log.write("status=failed reason=encrypted: password required "
                  "(retry with --password <user-password>)")
        log.flush()
        if is_temp and work_path != pdf:
            try:
                work_path.unlink(missing_ok=True)
            except OSError:
                pass
        return PdfResult(pdf.name, False, None, None, time.time() - t0,
                         reason="encrypted: password required "
                                "(retry with --password <user-password>)")

    _router_reason = ""

    def _try_engines(wp: Path) -> tuple[PdfResult | None, list[str], int]:
        """Run the engine chain against wp. Returns (success_result, all_reasons,
        page_load_failures). If success_result is None, all engines failed."""
        nonlocal _router_reason
        log.section("router")
        order, _router_reason = router.decide_with_reason(profile, cfg=router_config)
        log.write(f"engine order: {order}")
        log.write(f"router reason: {_router_reason}")

        reasons: list[str] = []
        page_load = 0
        for engine_name in order:
            engine = engines.get(engine_name)
            if engine is None:
                log.write(f"{engine_name}: not available in registry, skipping")
                continue
            log.section(f"engine: {engine_name}")
            try:
                # Pass page_count so SubprocessEngine can compute an
                # adaptive timeout — small PDFs get a tight bound (~26 min
                # for a 50p paper) instead of the 4-hour cap that masked
                # the 2026-04-29 marker hang.
                result = engine.convert(wp, stem_dir, page_count=profile.page_count)
                log.write(f"convert ok: {len(result.markdown)} chars, "
                          f"{len(result.images)} images, {result.elapsed:.1f}s")
            except EngineError as exc:
                msg = str(exc)
                log.write(f"{engine_name} failed: {exc}")
                log.write(traceback.format_exc())
                reasons.append(msg)
                if _is_page_load_failure(msg):
                    page_load += 1
                # Memory pressure exhausted the host — every other engine in
                # the chain will hit the same wall on the same host. Stop here
                # so the failure surfaces fast with the real reason instead of
                # being buried under N more engine failures.
                if exc.stage == "memory_pressure":
                    log.write("aborting engine chain: host memory pressure "
                              "won't be resolved by trying another engine")
                    break
                # CUDA driver is poisoned — every subsequent CUDA op on this
                # host is suspect until reboot. Critically, marker fallback
                # against a poisoned driver hangs the full per-engine timeout
                # without producing useful output (4 hours on the 2026-04-29
                # incident before the timeout fired). Short-circuit AND
                # propagate via the EngineError stage so the cli batch loop
                # aborts rather than queuing 20+ more guaranteed failures.
                # Write the standard "done / status=failed" trailer before
                # raising so the cli's debug.log reason scrape finds it on
                # the subprocess path (otherwise the reason comes back as
                # "unknown — no failure line").
                if exc.stage == "cuda_poisoned":
                    log.write("aborting engine chain: GPU CUDA context "
                              "poisoned, reboot required (re-raising so the "
                              "cli halts the batch)")
                    log.section("done")
                    log.write(f"status=failed reason=cuda_poisoned: {msg}")
                    log.flush()
                    raise
                continue
            except Exception as exc:
                msg = f"{engine_name}: {exc}"
                log.write(f"{engine_name} unexpected error: {exc}")
                log.write(traceback.format_exc())
                reasons.append(msg)
                if _is_page_load_failure(msg):
                    page_load += 1
                continue

            images_dir = stem_dir / "images"
            notes: list[str] = []
            cleaned = postprocess.clean(result.markdown, images_dir=images_dir, notes=notes)
            for n in notes:
                log.write(f"postprocess: {n}")

            report = quality.check(cleaned, profile=profile,
                                   images=result.images, images_dir=images_dir)
            log.write(f"quality: passed={report.passed} score={report.score:.2f} "
                      f"issues={report.issues}")

            if not report.passed:
                reasons.append(f"quality failed after {engine_name}: {report.issues}")
                continue

            md_path = stem_dir / f"{stem}.md"
            manifest = {
                "stem": stem,
                "engine": engine_name,
                "profile": asdict(profile),
                "quality_score": report.score,
                "quality_issues": report.issues,
                "elapsed_sec": time.time() - t0,
                "engine_order": order,
                "router_reason": _router_reason,
            }
            # F23: catch ENOSPC / disk errors here so the cli batch loop
            # gets a clean PdfResult(ok=False, reason="disk: ...") instead
            # of an uncaught traceback exiting the cli with code 1.
            try:
                _atomic_write(md_path, cleaned)
                _atomic_write(
                    stem_dir / "manifest.json",
                    json.dumps(manifest, indent=2))
            except OSError as exc:
                disk_reason = (f"disk: {exc.strerror or exc}"
                               f" (errno={exc.errno})")
                log.section("done")
                log.write(f"status=failed reason={disk_reason}")
                log.flush()
                return (PdfResult(name=pdf.name, ok=False, engine_used=None,
                                  quality_score=None,
                                  elapsed=time.time() - t0,
                                  reason=disk_reason),
                        reasons, page_load)
            log.section("done")
            log.write(f"status=ok engine={engine_name} score={report.score:.2f}")
            return (PdfResult(name=pdf.name, ok=True, engine_used=engine_name,
                              quality_score=report.score, elapsed=time.time() - t0,
                              chunks=0, reason=""), reasons, page_load)
        return (None, reasons, page_load)

    try:
        success, reasons, page_load = _try_engines(work_path)
        if success is not None:
            log.flush()
            return success

        # All engines failed. If ANY engine hit page-load damage on the
        # tier-1 sanitized output, retry once with a fully-rasterized
        # sanitize (tier 2). A rasterized PDF is just images — it bypasses
        # whatever tier-1 pdfwrite produced that trips pypdfium2. Don't
        # require every engine to match the page-load pattern: mineru can
        # fail with CUDA OOM while marker+docling hit page-load, and we
        # still want to retry (observed on a_non_random_walk_down_wall_street).
        attempted_engines = len([n for n in (router.decide(profile, cfg=router_config))
                                 if engines.get(n) is not None])
        retried_with_rasterize = False
        if (not skip_sanitize and attempted_engines > 0
                and page_load >= 1):
            log.section("retry: force_rasterize")
            log.write(f"{page_load}/{attempted_engines} engine(s) hit "
                      f"page-load damage; re-sanitizing with rasterize")
            # Release the tier-1 temp before we replace work_path.
            if is_temp and work_path != pdf:
                try:
                    work_path.unlink(missing_ok=True)
                except OSError:
                    pass
            sres2 = sanitize.run(pdf, force_rasterize=True, on_event=log.write)
            log.write(f"tier={sres2.tier} elapsed={sres2.elapsed:.1f}s ok={sres2.ok}")
            if sres2.ok:
                work_path = sres2.path
                is_temp = sres2.is_temp
                retried_with_rasterize = True
                success, reasons2, _ = _try_engines(work_path)
                if success is not None:
                    log.flush()
                    return success
                reasons = reasons + reasons2
            else:
                reasons.append("rasterize retry sanitize failed")

        last_reason = reasons[-1] if reasons else "no engines attempted"
        log.section("done")
        tag = " (after rasterize retry)" if retried_with_rasterize else ""
        log.write(f"status=failed{tag} reason={last_reason}")
        log.flush()
        return PdfResult(pdf.name, False, None, None, time.time() - t0,
                         reason=last_reason)
    finally:
        if is_temp and work_path != pdf:
            try:
                work_path.unlink(missing_ok=True)
            except OSError:
                pass
