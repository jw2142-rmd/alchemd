"""CLI + batch loop. Writes output/run.log summarizing every PDF.

Batch mode spawns a fresh subprocess per PDF. This isolates each conversion
from the next: marker/docling load pypdfium2 into the process and have been
observed to corrupt its state after a large doc, causing every subsequent
PDF to fail pypdfium2 load checks. A per-PDF subprocess guarantees each
file starts from a clean interpreter.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from alchemd import chunking, driver, env, router, slicing, ui
from alchemd.engines.base import EngineError
from alchemd.engines.subprocess_engine import SubprocessEngine

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_VENV = SCRIPT_DIR / "marker-env"
DEFAULT_INPUT = SCRIPT_DIR
DEFAULT_OUTPUT = SCRIPT_DIR / "output"

# Books over this page count are auto-sliced into 150-page parts before
# conversion. Marker has historical heap-corruption issues on very long
# books (the 449p `a_non_random_walk` only worked after the CHUNK_WORKERS=1
# fix; the 1667p `principles_of_neurology` only succeeded via manual
# slicing). 500 sits just above the largest book that ran cleanly in one
# shot, giving wall_street headroom while still catching anything bigger.
LARGE_PDF_PAGE_THRESHOLD = 500
SLICE_CHUNK_SIZE = 150

# Exit codes (Phase-1 finding F10). Documented so wrapping scripts (e.g.
# pdf_to_md.bat or a future scheduler) can distinguish "this batch had
# some per-PDF failures" from "stop the world, reboot the GPU".
EXIT_OK = 0
EXIT_USAGE = 1                    # bad args / missing input dir / not-a-PDF
EXIT_PER_PDF_FAILURES = 2         # batch finished, ≥1 PDF failed
EXIT_CUDA_ABORTED = 3             # GPU CUDA context poisoned; reboot required
EXIT_LOCKED = 4                   # another instance holds the output-dir lock
EXIT_DISK_ERROR = 5               # a PDF failed because of a disk-IO error
                                  # (ENOSPC, permission, etc.) — F23. Distinct
                                  # from PER_PDF_FAILURES so wrappers can
                                  # surface the disk-pressure category.

# Internal env var. Set by the parent before spawning per-PDF / per-slice
# child invocations of this same module so the child skips acquiring the
# output-dir lock the parent already holds (F17 — caught Phase-2 S3).
# Underscore prefix marks it as private; not part of the supported
# configuration surface.
_CHILD_HOLDS_LOCK_ENV = "_ALCHEMD_PARENT_HOLDS_LOCK"


def _normalize_stem(name: str) -> str:
    return name.lower().replace(" ", "_")


def _utc_now() -> str:
    """ISO-8601 UTC. Used in run.log entries so cross-host log-correlation
    works (Phase-1 finding F13)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class OutputDirLocked(RuntimeError):
    """Another process holds the lock on this output directory."""


class _OutputDirLock:
    """Advisory exclusive lock on `<output_dir>/.cli.lock`. Prevents two
    `alchemd` invocations from racing on the same output dir
    (Phase-1 finding F1: races on run.log, on per-PDF stem dirs, on the
    `<stem>.md` write — the symptom is a half-written cache-hit-eligible
    .md that quietly survives indefinitely).

    Implemented with `msvcrt.locking` on Windows and `fcntl.flock` elsewhere.
    The lock is best-effort: if the OS doesn't honour it (network mount,
    weird filesystem) the failure mode reverts to the existing race —
    which is what we have today, so no regression."""
    def __init__(self, output_dir: Path) -> None:
        self.path = output_dir / ".cli.lock"
        self._fh = None

    def __enter__(self) -> "_OutputDirLock":
        # Open for writing so we can record the holder's PID.
        self._fh = open(self.path, "w", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise OutputDirLocked(
                f"another process is already running against this output "
                f"directory (lock file: {self.path}). Wait for it to finish "
                f"or remove the lock file if you're sure no other instance "
                f"is running. OS error: {exc}") from exc
        # Record holder PID for diagnostics. Best-effort.
        try:
            self._fh.write(f"pid={os.getpid()} started={_utc_now()}\n")
            self._fh.flush()
        except OSError:
            pass
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                # Release before closing — lock is fd-bound.
                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            # POSIX: close releases flock automatically.
        finally:
            self._fh.close()
            self._fh = None
            try:
                self.path.unlink()
            except OSError:
                pass


def find_existing_output(pdf_path: Path, output_dir: Path) -> Path | None:
    if not output_dir.exists():
        return None
    pdf_mtime = pdf_path.stat().st_mtime
    target = _normalize_stem(pdf_path.stem)
    for subdir in output_dir.iterdir():
        if not subdir.is_dir():
            continue
        for md in subdir.glob("*.md"):
            if _normalize_stem(md.stem) == target and md.stat().st_mtime > pdf_mtime:
                return md
    return None


def build_parser() -> argparse.ArgumentParser:
    from alchemd import __version__

    p = argparse.ArgumentParser(
        prog="alchemd",
        description="alchemd — turn any pile of PDFs into perfect markdown. "
                    "Multi-engine pipeline (marker / docling / mineru) with "
                    "auto-routing per PDF profile, auto-slicing for large "
                    "books, and CUDA-poisoning safeguards.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Exit codes: 0=ok, 1=usage error, 2=≥1 PDF failed, "
            "3=cuda_aborted (reboot required), 4=output dir locked, "
            "5=disk error.\n"
            "Env vars: HF_HOME (model cache), TEMP/TMP/TMPDIR (engine "
            "scratch — point off C: for large books). "
            "See alchemd.bat for the canonical overnight invocation."),
    )
    p.add_argument("--version", action="version",
                   version=f"alchemd {__version__}")
    p.add_argument("pdf", nargs="?", default=None,
                   help="single PDF (name or path); otherwise every *.pdf "
                        "in --input-dir")
    p.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT,
                   help="directory containing PDFs to process")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT,
                   help="markdown + debug.log + run.log land here, one "
                        "subdir per PDF")
    p.add_argument("--venv", type=Path, default=DEFAULT_VENV,
                   help="venv to run env.check against (must contain torch, "
                        "marker, docling, mineru)")
    p.add_argument("--force", action="store_true",
                   help="re-process even when a cached output is newer than "
                        "the source PDF")
    p.add_argument("-y", "--yes", action="store_true",
                   help="non-interactive: skip env.check confirmation prompts")
    p.add_argument("--engine", choices=["auto", "marker", "docling", "mineru"],
                   default="auto",
                   help="override the router. auto: marker for prose, "
                        "docling for table-heavy, mineru for scanned. "
                        "Forcing one disables the fallback chain.")
    p.add_argument("--no-sanitize", action="store_true",
                   help="skip the sanitize tier (use only on PDFs you trust "
                        "to be pypdfium2-clean)")
    p.add_argument("--keep-temp", action="store_true",
                   help="retain sanitized intermediate PDFs and slice parts "
                        "for debugging")
    p.add_argument("--in-process", action="store_true",
                   help="process the batch in this interpreter, skipping "
                        "per-PDF subprocess isolation. Used internally by "
                        "the per-PDF subprocess loop; safe for users when "
                        "running a single PDF, otherwise prefer the default "
                        "subprocess mode for engine-state isolation.")
    p.add_argument("--no-auto-slice", action="store_true",
                   help=f"disable auto-slicing of PDFs over "
                        f"{LARGE_PDF_PAGE_THRESHOLD} pages (use the manual "
                        f"slice_pdf + merge_marker_parts flow instead)")
    return p


def _build_engines() -> dict[str, object]:
    """All engines run via SubprocessEngine: each loads in a fresh interpreter,
    so a failed engine's multi-GB model cache and CUDA state release before the
    next engine starts. Without this, attempts stack in RAM (10+ GB observed on
    a 449p book) and a force-killed engine with a live CUDA context can poison
    the GPU driver until reboot.

    Mineru is registered only if its CLI is resolvable on PATH. The Python
    package being importable is not enough — mineru's engine module shells
    out to the `mineru` console script, and registering an unusable engine
    just adds a guaranteed failure to the fallback chain (observed in the
    2026-04-28 v2 batch where every PDF ended with a misleading
    `[mineru:resolve]` reason after the real failure earlier in the chain).
    """
    engines: dict[str, object] = {
        "marker": SubprocessEngine("marker"),
        "docling": SubprocessEngine("docling"),
    }
    if shutil.which("mineru") is not None:
        engines["mineru"] = SubprocessEngine("mineru")
    return engines


def _router_override(engine: str) -> router.RouterConfig | None:
    # RouterConfig doesn't support pinning a single engine today; handled by
    # passing a one-entry order via a custom decide wrapper.
    return None


def _decide_override(engine: str):
    if engine == "auto":
        return None
    def _fn(profile, cfg=None):
        return [engine]
    return _fn


class CudaPoisonedError(RuntimeError):
    """Raised when a CUDA-poisoning failure is detected. The cli main loop
    catches this and aborts the rest of the batch — no point queuing more
    PDFs against a dead GPU."""


def _gpu_health_probe() -> None:
    """Allocate a 1-element tensor on CUDA. Raises CudaPoisonedError if the
    device is in a poisoned state (zero allocated, zero compute happening,
    every tiny op fails). <1 s when the GPU is healthy. Skipped silently if
    torch is unavailable or CUDA wasn't initialized — those conditions are
    surfaced upstream by env.check."""
    try:
        import torch
    except ImportError:
        return
    if not torch.cuda.is_available():
        return
    try:
        _ = torch.zeros(1, device="cuda")
        torch.cuda.synchronize()
    except Exception as exc:  # RuntimeError, OSError, etc.
        raise CudaPoisonedError(
            f"GPU health probe failed: {type(exc).__name__}: {exc}. "
            "CUDA driver is in a poisoned state — reboot the host before "
            "retrying. Subsequent engine subprocesses would hang the per-"
            "engine timeout instead of producing useful output."
        ) from exc


def _process_one_in_process(pdf_path: Path, output_dir: Path, args,
                            engines: dict[str, object] | None = None) -> tuple[bool, str, float, float]:
    """Run driver.process_one in THIS interpreter. Returns (ok, engine_used,
    quality_score, elapsed). Re-raises an EngineError(stage='cuda_poisoned')
    as CudaPoisonedError so the cli main loop can abort the batch."""
    if engines is None:
        engines = _build_engines()
    override_decide = _decide_override(args.engine)

    original_decide = router.decide
    if override_decide is not None:
        router.decide = override_decide  # type: ignore[assignment]
    try:
        try:
            r = driver.process_one(pdf_path, output_dir, engines=engines,
                                   skip_sanitize=args.no_sanitize)
        except EngineError as exc:
            if exc.stage == "cuda_poisoned":
                raise CudaPoisonedError(str(exc)) from exc
            raise
    finally:
        router.decide = original_decide  # type: ignore[assignment]
    return (r.ok, r.engine_used or "", r.quality_score or 0.0, r.elapsed)


def _process_one_subprocess(pdf_path: Path, output_dir: Path, args,
                            input_dir_override: Path | None = None) -> tuple[bool, str, float, float]:
    """Spawn a fresh interpreter to process exactly one PDF. Returns (ok,
    engine_used, quality_score, elapsed) derived from manifest.json on success.

    `input_dir_override` lets callers (e.g. the auto-slice path) point the
    subprocess at a slice-staging dir without mutating the parent's args."""
    t0 = time.time()
    input_dir = input_dir_override or args.input_dir
    cmd = [
        sys.executable, "-m", "alchemd",
        pdf_path.name,
        "--input-dir", str(input_dir),
        "--output-dir", str(output_dir),
        "-y", "--in-process",
        # The slice-loop already disables auto-slice for its children; for
        # ordinary per-PDF subprocesses, propagating the parent's flag
        # keeps behavior consistent.
        "--no-auto-slice",
    ]
    if args.force:
        cmd.append("--force")
    if args.engine != "auto":
        cmd.extend(["--engine", args.engine])
    if args.no_sanitize:
        cmd.append("--no-sanitize")
    if args.keep_temp:
        cmd.append("--keep-temp")

    # The parent holds <output_dir>/.cli.lock. Tell the child to skip the
    # lock acquire so it doesn't deadlock the batch on its own parent
    # (F17). External concurrent invocations don't inherit this env var
    # so F1's same-output-dir guard still fires for them.
    child_env = os.environ.copy()
    child_env[_CHILD_HOLDS_LOCK_ENV] = "1"
    rc = subprocess.call(cmd, env=child_env)
    elapsed = time.time() - t0

    manifest_path = output_dir / pdf_path.stem / "manifest.json"
    if rc == 0 and manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        return (True, m.get("engine", "unknown"),
                float(m.get("quality_score", 0.0)), elapsed)
    return (False, "", 0.0, elapsed)


def _process_sliced(pdf_path: Path, output_dir: Path, args,
                    page_count: int) -> tuple[bool, str, float, float]:
    """Slice -> per-slice convert (each in its own subprocess for engine-state
    isolation) -> merge. Each slice goes through the normal router, so a
    table-heavy chapter inside a long book can route to docling while the
    rest of the book uses marker.

    Returns (ok, engine_used_summary, mean_quality_score, elapsed)."""
    t0 = time.time()
    stem = pdf_path.stem
    # Slices live next to the merged output so cleanup is co-located, and
    # `--keep-temp` retains them when set (mirroring sanitize-tier behavior).
    slices_dir = output_dir / ".slices" / stem
    if slices_dir.exists():
        shutil.rmtree(slices_dir)
    slices_dir.mkdir(parents=True, exist_ok=True)

    ui.info(f"auto-slice: {page_count}p > {LARGE_PDF_PAGE_THRESHOLD}p — "
            f"splitting into {SLICE_CHUNK_SIZE}-page parts")
    slice_paths = slicing.slice_pdf(pdf_path, slices_dir, SLICE_CHUNK_SIZE)
    # Stamp every slice with the source PDF's mtime so cached per-part
    # outputs from a prior run still satisfy `find_existing_output`'s
    # `md.mtime > pdf.mtime` check after a re-slice. Without this, every
    # retry of a sliced book re-converts every part from scratch even when
    # 11/12 succeeded last time (Phase-1 finding F2).
    pdf_mtime = pdf_path.stat().st_mtime
    for sp in slice_paths:
        try:
            os.utime(sp, (pdf_mtime, pdf_mtime))
        except OSError:
            pass
    ui.info(f"auto-slice: {len(slice_paths)} parts")

    per_slice_engines: list[str] = []
    per_slice_scores: list[float] = []
    any_slice_failed = False
    failed_slice_path: Path | None = None

    for slice_path in slice_paths:
        print(f"  {slice_path.name}")
        ok, engine_used, quality_score, _elapsed = _process_one_subprocess(
            slice_path, output_dir, args, input_dir_override=slices_dir)
        if ok:
            per_slice_engines.append(engine_used)
            per_slice_scores.append(quality_score)
            ui.ok(f"  {slice_path.name} via {engine_used} "
                  f"(score {quality_score:.2f})")
        else:
            ui.err(f"  {slice_path.name} failed")
            any_slice_failed = True
            failed_slice_path = slice_path
            break  # Don't waste time on remaining slices if one breaks

    elapsed = time.time() - t0

    if any_slice_failed:
        # F31: synthesize a debug.log at the merged-stem path that points
        # at the failed slice with its actual reason. Without this, the
        # cli main loop's `_read_last_failure_reason(output_dir/<stem>/debug.log)`
        # finds nothing and reports the misleading
        # "unknown (no debug.log written...)".
        if failed_slice_path is not None:
            slice_debug = output_dir / failed_slice_path.stem / "debug.log"
            slice_reason = _read_last_failure_reason(slice_debug)
            merged_stem_dir = output_dir / stem
            merged_stem_dir.mkdir(parents=True, exist_ok=True)
            (merged_stem_dir / "debug.log").write_text(
                f"=== sliced run aborted ===\n"
                f"failed slice: {failed_slice_path.name}\n"
                f"slice debug.log: {slice_debug}\n"
                f"=== done ===\n"
                f"status=failed reason=slice {failed_slice_path.name}: "
                f"{slice_reason}\n",
                encoding="utf-8",
            )
        # Leave slice intermediates in place so the operator can re-run
        # the failed slice manually with debug.log preserved.
        return (False, "", 0.0, elapsed)

    try:
        slicing.merge_parts(stem, output_dir, output_dir)
    except FileNotFoundError as e:
        ui.err(f"merge failed: {e}")
        return (False, "", 0.0, elapsed)

    # Cleanup per-part output dirs and slice PDFs once merge succeeds —
    # everything they contained is now under output_dir/<stem>/.
    if not args.keep_temp:
        for slice_path in slice_paths:
            part_out = output_dir / slice_path.stem
            if part_out.is_dir():
                shutil.rmtree(part_out, ignore_errors=True)
        shutil.rmtree(slices_dir, ignore_errors=True)
        slices_root = output_dir / ".slices"
        try:
            slices_root.rmdir()  # only succeeds if empty
        except OSError:
            pass

    engines_summary = "+".join(sorted(set(per_slice_engines))) or "unknown"
    mean_score = (sum(per_slice_scores) / len(per_slice_scores)
                  if per_slice_scores else 0.0)
    return (True, f"sliced({engines_summary})", mean_score, elapsed)


def main() -> int:
    # F29: line-buffer stdout so parent-side ui prints land in the redirected
    # log file as they happen, instead of being held in a 4KB block buffer
    # behind child subprocess output. Without this, S3 captured a stdout log
    # that contained child output but NO parent header lines (parent was
    # killed before flushing). reconfigure() is a no-op on Python <3.7 and
    # safe to call even when stdout has already been written to.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    args = build_parser().parse_args()

    input_dir: Path = args.input_dir.expanduser().resolve()
    output_dir: Path = args.output_dir.expanduser().resolve()
    venv: Path = args.venv.expanduser().resolve()
    args.input_dir = input_dir
    args.output_dir = output_dir

    if not input_dir.is_dir():
        ui.err(f"Input directory does not exist: {input_dir}")
        return EXIT_USAGE

    # F27: fast-fail not-a-PDF before the lock acquire and env.check (which
    # imports torch and takes ~5s on Windows). Without this, a typo'd
    # filename burns the env.check time before reporting the error.
    if args.pdf:
        candidate = Path(args.pdf).expanduser()
        pdf_resolved = candidate if candidate.is_absolute() else (input_dir / candidate)
        pdf_resolved = pdf_resolved.resolve()
        if not pdf_resolved.is_file() or pdf_resolved.suffix.lower() != ".pdf":
            ui.err(f"Not a PDF file: {pdf_resolved}")
            return EXIT_USAGE

    output_dir.mkdir(parents=True, exist_ok=True)

    # Per-PDF / per-slice child invocations are spawned by the parent's
    # `_process_one_subprocess` against the same output dir. Re-acquiring
    # the parent's lock from the child blocks every PDF in the batch with
    # EXIT_LOCKED — the F17 regression caught on the first Phase-2 stress
    # test (S3). The parent sets `_CHILD_HOLDS_LOCK_ENV` before spawning so
    # children skip the acquire while still rejecting genuine concurrent
    # external invocations.
    if os.environ.get(_CHILD_HOLDS_LOCK_ENV) == "1":
        return _run_batch(args, input_dir, output_dir, venv)

    try:
        with _OutputDirLock(output_dir):
            return _run_batch(args, input_dir, output_dir, venv)
    except OutputDirLocked as exc:
        ui.err(str(exc))
        return EXIT_LOCKED


def _run_batch(args, input_dir: Path, output_dir: Path, venv: Path) -> int:
    """Inner main() body. Split out so the output-dir lock wraps it cleanly."""
    # F30: children spawned by _process_one_subprocess inherit the parent's
    # env.check result — repeating env.check per PDF burns ~5s × N (Windows)
    # and floods stdout with redundant "OK marker found" lines. Same env-var
    # bypass as F17/F18.
    if os.environ.get(_CHILD_HOLDS_LOCK_ENV) != "1":
        env.check(venv, args.yes)

    if args.pdf:
        candidate = Path(args.pdf).expanduser()
        pdf_path = candidate if candidate.is_absolute() else (input_dir / candidate)
        pdf_path = pdf_path.resolve()
        if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
            ui.err(f"Not a PDF file: {pdf_path}")
            return EXIT_USAGE
        pdfs = [pdf_path]
        # Single-PDF invocation is always in-process (it's already isolated).
        batch_isolated = False
    else:
        pdfs = sorted(input_dir.glob("*.pdf"))
        if not pdfs:
            ui.warn(f"No PDFs found in {input_dir}")
            return EXIT_OK
        # Batch mode isolates each PDF in its own subprocess unless told otherwise.
        batch_isolated = not args.in_process

    note = f" {ui.YELLOW}(--force){ui.RESET}" if args.force else ""
    iso = "" if batch_isolated else f" {ui.YELLOW}(in-process){ui.RESET}"
    print(f"{ui.BOLD}Processing {len(pdfs)} PDF(s){ui.RESET}{note}{iso}\n")

    run_log = output_dir / "run.log"
    run_log_lines: list[str] = [f"# run started {_utc_now()}"]
    batch_t0 = time.time()

    all_chunks: list[dict] = []
    in_process_engines: dict[str, object] | None = None if batch_isolated else _build_engines()

    any_failed = False

    cuda_aborted = False
    disk_error_seen = False

    # Per-PDF status records for the end-of-batch summary (Phase-1 finding F14).
    # Each: {"name": str, "status": str, "engine": str, "elapsed": float}
    summary: list[dict] = []

    for pdf_idx, pdf_path in enumerate(pdfs, start=1):
        # F28: index/total header per PDF so a redirected log answers
        # "where am I in the batch?" without spelunking timestamps.
        prefix = f"[{pdf_idx}/{len(pdfs)}] " if len(pdfs) > 1 else ""
        print(f"{ui.BOLD}{prefix}{pdf_path.name}{ui.RESET}")

        if not args.force:
            existing = find_existing_output(pdf_path, output_dir)
            if existing:
                ui.info(f"cached -- {existing.name}")
                md_text = existing.read_text(encoding="utf-8", errors="replace")
                manifest_path = existing.parent / "manifest.json"
                engine_used = "unknown"
                quality_score = 0.0
                if manifest_path.exists():
                    m = json.loads(manifest_path.read_text(encoding="utf-8"))
                    engine_used = m.get("engine", "unknown")
                    quality_score = float(m.get("quality_score", 0.0))
                chunks = chunking.chunk(md_text, source=pdf_path.name,
                                        engine=engine_used, quality_score=quality_score)
                all_chunks.extend(chunks)
                ui.ok(f"{len(chunks)} chunks (cached)")
                run_log_lines.append(
                    f"{pdf_path.name}\tcached\t{engine_used}\tscore={quality_score:.2f}")
                summary.append({"name": pdf_path.name, "status": "cached",
                                "engine": engine_used, "elapsed": 0.0})
                print()
                continue

        # GPU health probe runs BEFORE we spend minutes on a per-PDF
        # subprocess. A poisoned driver from a prior PDF (or another job
        # on the box) would make every engine attempt a 4-hour timeout
        # waste. <1 s when healthy.
        try:
            _gpu_health_probe()
        except CudaPoisonedError as exc:
            ui.err(f"aborting batch: {exc}")
            run_log_lines.append(
                f"{pdf_path.name}\tABORTED\treason=cuda_poisoned (probe): {exc}")
            cuda_aborted = True
            any_failed = True
            break

        # Auto-slice large PDFs into 150-page parts before conversion
        # (configurable threshold; opt-out via --no-auto-slice). The slice
        # path takes over the per-PDF flow entirely: it slices, processes
        # each part through _process_one_subprocess, and merges the parts
        # into output_dir/<stem>/. Caching uses the merged output, so a
        # second run hits the cache the same way as a small-PDF run.
        # Auto-slice is independent of engine choice: --engine marker on a
        # 1500p book still benefits from being sliced, it just runs marker
        # on every slice instead of router-deciding per slice.
        sliced = False
        if not args.no_auto_slice:
            try:
                pages = slicing.page_count(pdf_path)
            except Exception as exc:
                ui.warn(f"page_count probe failed ({exc}); skipping auto-slice")
                pages = 0
            if pages > LARGE_PDF_PAGE_THRESHOLD:
                ok, engine_used, quality_score, elapsed = _process_sliced(
                    pdf_path, output_dir, args, page_count=pages)
                sliced = True

        if not sliced:
            try:
                if batch_isolated:
                    ok, engine_used, quality_score, elapsed = _process_one_subprocess(
                        pdf_path, output_dir, args)
                else:
                    ok, engine_used, quality_score, elapsed = _process_one_in_process(
                        pdf_path, output_dir, args, engines=in_process_engines)
            except CudaPoisonedError as exc:
                ui.err(f"aborting batch: {exc}")
                run_log_lines.append(
                    f"{pdf_path.name}\tABORTED\treason=cuda_poisoned: {exc}")
                cuda_aborted = True
                any_failed = True
                break

        if ok:
            md_file = output_dir / pdf_path.stem / f"{pdf_path.stem}.md"
            md_text = md_file.read_text(encoding="utf-8", errors="replace")
            chunks = chunking.chunk(md_text, source=pdf_path.name,
                                    engine=engine_used or "unknown",
                                    quality_score=quality_score)
            all_chunks.extend(chunks)
            ui.ok(f"{len(chunks)} chunks via {engine_used} (score {quality_score:.2f})")
            run_log_lines.append(
                f"{pdf_path.name}\tok\t{engine_used}\tscore={quality_score:.2f}\telapsed={elapsed:.1f}s")
            summary.append({"name": pdf_path.name, "status": "ok",
                            "engine": engine_used, "elapsed": elapsed})
        else:
            debug_path = output_dir / pdf_path.stem / "debug.log"
            reason = _read_last_failure_reason(debug_path)
            ui.err(f"failed -- {reason}")
            run_log_lines.append(
                f"{pdf_path.name}\tFAILED\treason={reason}\tdebug={debug_path}")
            summary.append({"name": pdf_path.name, "status": "failed",
                            "engine": "", "elapsed": 0.0})
            any_failed = True
            # Subprocess path: cuda_poisoned shows up in the per-PDF
            # debug.log reason. Detect and abort the same as the in-process
            # path. This is what the next-PDF GPU probe would also catch,
            # but bailing immediately saves the probe round-trip.
            if "cuda_poisoned" in reason or "cuda context poisoned" in reason.lower():
                ui.err("aborting batch: GPU CUDA context poisoned, "
                       "reboot required before retrying")
                cuda_aborted = True
                break
            # F23: a per-PDF disk error escalates the batch's exit code so
            # a wrapping scheduler can distinguish "this PDF failed for
            # content reasons (try a different engine)" from "the host is
            # out of disk (free space, then retry)".
            if reason.startswith("disk:") or "disk:" in reason.lower():
                disk_error_seen = True

        print()

    # F18: chunks.json and run.log are batch-level artifacts owned by the
    # parent. Per-PDF child invocations (spawned by _process_one_subprocess)
    # share this code path but should not touch these files — otherwise a
    # killed parent leaves behind a single-PDF chunks.json from the last
    # child that ran (S3 stress, 2026-04-30).
    is_child = os.environ.get(_CHILD_HOLDS_LOCK_ENV) == "1"

    if all_chunks and not is_child:
        (output_dir / "chunks.json").write_text(
            json.dumps(all_chunks, indent=2, ensure_ascii=False), encoding="utf-8")
        ui.info(f"chunks.json -- {len(all_chunks)} chunks")

    if cuda_aborted:
        run_log_lines.append("# batch aborted: cuda_poisoned (reboot required)")
    run_log_lines.append(f"# run finished {_utc_now()}")
    # Append (not overwrite) so multiple runs/day aren't lost (F3).
    # Separator line keeps batches visually distinct.
    if not is_child:
        with open(run_log, "a", encoding="utf-8") as fh:
            if run_log.stat().st_size > 0:
                fh.write("\n")
            fh.write("\n".join(run_log_lines) + "\n")

    # Aggregate summary to stdout (Phase-1 finding F14).
    _print_batch_summary(summary, time.time() - batch_t0,
                         cuda_aborted=cuda_aborted)

    # Distinct exit codes let a wrapping batch script (or a future
    # scheduler) tell apart:
    #   - this PDF failed (engine / content reasons)         → 2
    #   - the GPU is poisoned, stop running until reboot     → 3
    #   - this PDF failed because the host is out of disk    → 5 (F23)
    if cuda_aborted:
        return EXIT_CUDA_ABORTED
    if disk_error_seen:
        return EXIT_DISK_ERROR
    return EXIT_OK if not any_failed else EXIT_PER_PDF_FAILURES


def _print_batch_summary(summary: list[dict], total_elapsed: float,
                         cuda_aborted: bool) -> None:
    """End-of-batch human-readable aggregate (Phase-1 finding F14)."""
    n_ok = sum(1 for s in summary if s["status"] == "ok")
    n_cached = sum(1 for s in summary if s["status"] == "cached")
    n_failed = sum(1 for s in summary if s["status"] == "failed")
    print(f"\n{ui.BOLD}Batch summary{ui.RESET}")
    print(f"  ok={n_ok}  cached={n_cached}  failed={n_failed}  "
          f"total={total_elapsed:.1f}s")
    converted = [s for s in summary if s["status"] == "ok"]
    if converted:
        slowest = max(converted, key=lambda s: s["elapsed"])
        print(f"  slowest: {slowest['name']} via {slowest['engine']} "
              f"({slowest['elapsed']:.1f}s)")
    if n_failed:
        failed = [s["name"] for s in summary if s["status"] == "failed"]
        print(f"  failed: {', '.join(failed)}")
    if cuda_aborted:
        print(f"  {ui.YELLOW}cuda_aborted: reboot required before next run"
              f"{ui.RESET}")


_TIMESTAMP_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s*")
_STATUS_FAILED_PREFIX_RE = re.compile(r"^status=failed\s+reason=")


def _read_last_failure_reason(debug_path: Path) -> str:
    """Scrape the last 'status=failed' or 'reason=' line from debug.log.
    Falls back to 'unknown' if the log is missing or malformed.

    F19: strips the leading [ISO-8601-Z] prefix that `_DebugLog.write`
    prepends and the redundant 'status=failed reason=' prefix, since both
    leak ugly into the user-facing FAIL line."""
    if not debug_path.exists():
        return "unknown (no debug.log written — process may have crashed hard)"
    try:
        lines = debug_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "unknown"
    for line in reversed(lines):
        if "status=failed" in line or "reason=" in line:
            stripped = _TIMESTAMP_PREFIX_RE.sub("", line.strip())
            return _STATUS_FAILED_PREFIX_RE.sub("", stripped)
    return "unknown (no failure line in debug.log)"
