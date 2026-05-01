# Pipeline Fixes Plan — Phase-2/3 follow-up

**Date:** 2026-05-01
**Inputs:** `docs/superpowers/specs/2026-04-30-pipeline-eval-findings.md` (31 findings F1–F31)
**Status:** **Draft (not executed).** F1–F16 already shipped in commit `6c4e115`. F17 already shipped as a blocker fix in `0006b49`. This plan covers F18–F31.

## Goal

Land the remaining S1/S2 robustness findings, then the S3 UX cleanups. Out of scope: re-architecting the parent/child cli split, adding new engines, anything that needs a model retrain.

## Sequencing

S1/S2 first (correctness / batch-aborting), S3 second (annoying), S4 last (cosmetic). Within a tier, order by code-locality so similar files land in the same commit.

## Tier 1 — S1/S2 (must-fix)

### F20 (R, S2) — Encrypted-PDF detection
**File:** `process_papers_v3/preflight.py`, `process_papers_v3/driver.py`, `process_papers_v3/cli.py`.
**Change:** preflight.probe() additionally probes the ORIGINAL pdf for `pikepdf.PasswordError` and exposes a `is_originally_encrypted` field on `PdfProfile`. driver.process_one short-circuits with `EngineError(stage="encrypted")` if set, returning a fail reason `encrypted: password required (use --password to supply user password)`. New cli flag `--password`. New exit code or reuse EXIT_PER_PDF_FAILURES (the latter — encryption is a per-PDF problem, not a batch one).
**Test:** Reproduce S4: `Testing_stress/encrypted.pdf` should fail with `reason=encrypted: password required`, NOT with `emptiness`. Add a unit test on preflight.probe against a pikepdf-encrypted fixture.

### F23 (R, S2) — Catch OSError on `_atomic_write`
**File:** `process_papers_v3/driver.py`.
**Change:** Wrap `_atomic_write(md_path, ...)` and `_atomic_write(manifest_path, ...)` in `try/except OSError`; on failure, log `status=failed reason=write: <errno>: <strerror>` to debug.log and return `PdfResult(ok=False, ...)`. New `EXIT_DISK_ERROR = 5` for batch-level disk failures (any per-PDF disk failure → batch returns 5 instead of 2).
**Test:** Repurpose `scripts/s2_fault_inject_disk_full.py` into a unit test that asserts the fault path produces a clean exit (not a Python traceback).

### F24 (R, S3) — Cleanup orphan `.tmp` on `_atomic_write` failure
**File:** `process_papers_v3/driver.py`.
**Change:** `try/finally tmp.unlink(missing_ok=True)` around the write+replace pair. The `os.replace` consumes the tmp on success, so the unlink only runs after a failure.
**Test:** Inject OSError into `tmp.write_text`; assert no `.md.tmp` survives in the stem dir.

### F18 (R, S3) — Children skip parent-only writes (chunks.json, run.log)
**File:** `process_papers_v3/cli.py`.
**Change:** In `_run_batch`, gate the `chunks.json` write and the `run.log` append on `os.environ.get(_CHILD_HOLDS_LOCK_ENV) != "1"`. Children carry no batch-level state.
**Test:** Run `_run_batch` with the env var set; assert neither file is written. Add to `test_phase1_fixes.py`.

### F31 (R, S3) — Sliced-PDF failure reason looks in wrong stem dir
**File:** `process_papers_v3/cli.py`.
**Change:** In `_process_sliced`, when `any_slice_failed`, propagate the failed slice's `debug.log` content (last `status=failed reason=...` line) up so the cli main loop can surface it. Either by writing a synthetic `<output_dir>/<merged_stem>/debug.log` summary, or by changing the contract so `_process_sliced` returns a `(ok, ...)` tuple that includes a reason string. ~20 lines.
**Test:** Mock `_process_one_subprocess` to fail on slice 2 with a known debug.log; assert the surfaced reason names slice 2 and includes its actual failure text.

## Tier 2 — S3 (annoying)

### F19 (U, S3) — Strip timestamp prefix from FAIL reason
**File:** `process_papers_v3/cli.py`.
**Change:** In `_read_last_failure_reason`, strip a leading `[YYYY-MM-DDTHH:MM:SSZ] ` prefix before returning. Test with a synthetic debug.log line.

### F21 (R, S3) — CPU-aware adaptive timeout
**File:** `process_papers_v3/engines/subprocess_engine.py`.
**Change:** `adaptive_timeout` checks `torch.cuda.is_available()` (cached at module scope to avoid repeated probes) and applies a 6× multiplier on CPU. Or expose `PROCESS_PAPERS_V3_CPU_TIMEOUT_MULT` env var.
**Test:** Mock cuda-availability to False, assert timeout grows 6×.

### F26 (U, S3) — README single-PDF example
**File:** `README.md`. Add the single-PDF invocation alongside batch.

### F28 (U, S3) — Live progress per PDF
**File:** `process_papers_v3/cli.py`, `process_papers_v3/driver.py`.
**Change:** Print `[i/N] <name> (<page_count>p, route=<engine>)` header before each PDF; mirror sanitize/preflight/router/engine stage transitions to stdout when `sys.stdout.isatty()`.

### F29 (U, S3) — Line-buffer parent stdout
**File:** `process_papers_v3/cli.py`.
**Change:** `sys.stdout.reconfigure(line_buffering=True)` at the top of `main()`. ~3 lines.

## Tier 3 — S4 (nice to have)

### F22 (U, S4) — CPU-only warning remediation guidance
**File:** `process_papers_v3/env.py`.

### F25 (U, S4) — `--in-process` help text reword
**File:** `process_papers_v3/cli.py`.

### F27 (U, S4) — Move not-a-PDF check into `main()` for fast-fail
**File:** `process_papers_v3/cli.py`. Move the `args.pdf` resolve+exists+suffix check out of `_run_batch` and into `main()` before the lock acquire and env.check.

### F30 (U, S4) — Children skip `env.check`
**File:** `process_papers_v3/cli.py`. Same env-var bypass as F17/F18: `if os.environ.get(_CHILD_HOLDS_LOCK_ENV) == "1": skip env.check`. Saves 30-50s on a 10-PDF batch (one env.check per PDF in subprocess mode) and removes redundant log lines. Two-line change.

## Validation

- Re-run the Phase-2 stress matrix on each fixed PDF (S2-S5, S7-S10) and check:
  - F20 fix: S4 (encrypted) now reports `encrypted: password required`.
  - F23 fix: S2 fault inject exits cleanly with EXIT_DISK_ERROR.
  - F18 fix: kill-mid-batch state shows no child-clobbered chunks.json/run.log.
  - All other PASS results unchanged.
- Re-run `test_phase1_fixes.py` plus all new tests.
- Re-run the v2 7-PDF set (S10 from this evaluation) to confirm no quality regression.

## Done when

- Every F18–F29 finding is checked off above with a commit ref.
- Findings doc is annotated with `**Fix landed:** <commit>` against each.
- The Phase-2 stress matrix re-runs cleanly with no new failures.
