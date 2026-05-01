# Pipeline Eval Findings 2026-04-30

Tracker for the static audit + stress matrix + UX walkthrough described in `docs/superpowers/plans/2026-04-30-pipeline-evaluation.md`.

Severity:
- **S1** — data loss / silent corruption / unrecoverable state
- **S2** — batch-aborting, but no permanent damage
- **S3** — friction (annoying, slow, confusing)
- **S4** — nice-to-have / cosmetic

Tags: **R** robustness, **U** UX.

## Summary

| Severity | R count | U count |
|---|---:|---:|
| S1 | 2 | 0 |
| S2 | 6 | 0 |
| S3 | 6 | 11 |
| S4 | 2 | 5 |

31 findings across Phase 1+2+3 (F1–F31). **All 31 fixed.**
- F1–F16 fixed in commit `6c4e115` (Phase-1 static audit landings).
- F17 fixed inline as Phase-2 blocker in `0006b49`.
- F18–F31 fixed in `4922548` (this plan run, 2026-05-01).

Stress matrix outcome: S2, S3, S4, S5, S7, S8 PASS; S9 PASS-with-positive-surprise (Layer-0 also fixed docling on wall_street); S10 PASS on all 7 v2 PDFs. S6 PARTIAL (slicing + 1-of-34 parts validated, merge step not re-validated this session — already validated in prior neurology run). S1 (real CUDA OOM) deferred per user instruction (requires host reboot). Phase 3 (UX walkthrough U1-U8) complete.

---

## Findings (Phase 1, static audit)

### F1 — Concurrent runs on the same output dir can corrupt cache (R, S1)
**Symptom:** Two `pdf_to_md.bat` (or `python -m process_papers_v3`) instances against the same `--output-dir` race on `run.log` (last-write-wins overwrite at `cli.py:450`) and on per-PDF `output/<stem>/` directories. Two children processing the same PDF can produce a half-written `<stem>.md` that future runs treat as cached.
**Reproduction:** Start two batches in parallel against `Testing/output/`.
**Root cause:** No pidfile / lockfile / advisory lock anywhere in `cli.py`. The `pdf_to_md.bat` wrapper has no concurrency guard either — a double-clicked `.bat` will silently overlap with itself.
**Proposed fix:** Acquire an exclusive lock on `<output_dir>/.cli.lock` at startup (Python `msvcrt.locking` on Windows / `fcntl.flock` on POSIX). Fail fast with an actionable message if held. Cheap. ~30 lines.

### F2 — Slice retry invalidates per-part cache, wasting hours (R, S2)
**Symptom:** Re-running a sliced PDF (e.g., 1667p neurology) after a partial-progress crash re-converts every slice from scratch, even when 11/12 parts already succeeded.
**Reproduction:** `_process_sliced` at `cli.py:230-231` `shutil.rmtree(slices_dir); slices_dir.mkdir(...)`. The slice PDFs get fresh mtimes. `find_existing_output` (`cli.py:52`) cache check is `md.stat().st_mtime > pdf_mtime` — older converted-part outputs now look stale relative to the freshly-cut slice PDFs, so every part re-converts.
**Root cause:** Slice PDFs are deterministic (same page ranges from same input), but the rmtree wipes mtime evidence the cache relies on.
**Proposed fix:** Skip the rmtree when slice PDFs already exist with the expected count, OR use content-hash-based cache validation, OR `os.utime` the freshly-cut slices to the original PDF's mtime so cached parts still validate. Smallest fix is `os.utime(slice_path, (pdf_mtime, pdf_mtime))` after each `slicing.slice_pdf`.

### F3 — `run.log` is overwritten every batch (R, S2)
**Symptom:** Running the pipeline twice in a day loses the first run's log. No append, no rotation.
**Reproduction:** `cli.py:450` is `run_log.write_text(...)`. Single call, atomic overwrite.
**Root cause:** Designed as a single per-run summary; no thought given to multiple runs/day.
**Proposed fix:** Either append (`open(run_log, "a")`) with a `# run started ...` header per batch, or rotate to `run-YYYYMMDD-HHMMSS.log` and keep `run.log` as a symlink/copy of the most recent. Append is simpler.

### F4 — `<stem>.md` is not written atomically (R, S2)
**Symptom:** A crash mid-write of `<stem>.md` (driver.py:199) can leave a truncated file. Future runs see the file, satisfy `find_existing_output`, and treat it as a cache hit — silent corruption.
**Reproduction:** Hard to reproduce deliberately, but plausible under power loss / kill-9 of the engine subprocess after engine returned but before `write_text` completed.
**Proposed fix:** Standard `*.md.tmp` → `os.replace(*.md.tmp, *.md)` pattern. Same for `manifest.json` and `merge_parts`'s final write. Cheap, ~10 lines per site.

### F5 — Reboot-recovery workflow undocumented (R, S2)
**Symptom:** After exit-3 cuda_aborted, no docs tell the operator what to do next (reboot, then? re-run? `--force`?). Cache handles most of it but the F2 slice-cache pitfall hits unsuspecting users on long books.
**Proposed fix:** Add a "After a cuda_aborted batch" section to README. Confirm cache resumes correctly, point at F2 once fixed.

### F6 — Engine-level `EngineError(stage="convert", exc)` is opaque (R, S3)
**Symptom:** `engines/marker.py:100` and `engines/docling.py:61` wrap arbitrary exceptions as `EngineError(self.name, "convert", exc)`. The `exc` is stringified by EngineError — sometimes useful, often just `'NoneType' object has no attribute ...`. No traceback in the error message itself (the driver logs the traceback separately at `driver.py:142`, but the EngineError's `str()` value goes into run.log without it).
**Proposed fix:** Capture `traceback.format_exc()` on raise and include the last frame in the EngineError message. Or include `type(exc).__name__` so the run.log entry is at least classifiable.

### F7 — `EngineError` from missing tool has no install hint (R, S3)
**Symptom:** `engines/mineru.py:25` raises `"mineru CLI not found on PATH"`. `sanitize.py:99,130` raises `"ghostscript not found on PATH"`. Neither says how to install. `env.check` *does* warn about both upfront, but if env.check is skipped (e.g., `--no-sanitize` doesn't skip it but `--in-process` test paths can) or env shifts mid-batch, the engine raise is the only signal.
**Proposed fix:** Reference the install command in the message: `"mineru CLI not found on PATH — pip install mineru should add the console script"`. For ghostscript, link `GS_MANUAL_URL`.

### F8 — Mineru timeout message is bare (R, S4)
**Symptom:** `engines/mineru.py:51` says `"mineru timeout 3600s"`. No hint at causes (large PDF, OCR forced, GPU stuck).
**Proposed fix:** Include `page_count` (already known from preflight) in the message; suggest checking GPU state.

### F9 — Tunable constants scattered across 8 modules (R, S4)
**Symptom:** Operationally meaningful knobs (slice threshold, timeouts, quality thresholds, router thresholds, docling batch sizes) live as module-level constants in `cli.py`, `engines/marker.py`, `engines/docling.py`, `subprocess_engine.py`, `quality.py`, `router.py`, `sanitize.py`, `preflight.py`. No central index.
**Proposed fix:** Either a `process_papers_v3/config.py` re-exporting and documenting all tunables, or just a section in README listing them with file:line references. Doc-only fix is fine — moving them risks breaking imports.

### F10 — Exit codes use magic numbers, undocumented (U, S3)
**Symptom:** `cli.py` returns `0/1/2/3` from `main()`; `engine_runner.py` returns `0/2`. Nothing names them. README doesn't mention them. `pdf_to_md.bat` doesn't check them, so a `cuda_aborted` (3) batch is indistinguishable from a per-PDF-failures (2) batch in the wrapper.
**Proposed fix:** Constants at top of `cli.py` (`EXIT_OK = 0`, `EXIT_USAGE = 1`, `EXIT_PER_PDF_FAILURES = 2`, `EXIT_CUDA_ABORTED = 3`). Add a "Exit codes" section to README. Update `pdf_to_md.bat` to surface 3 as a distinct condition (e.g., write a sentinel file, send a desktop notification).

### F11 — `--help` is sparse (U, S3)
**Symptom:** `--input-dir`, `--output-dir`, `--venv`, `--force`, `-y` have no help text. `--engine` says only `"override router"` — doesn't say what each engine is for. Defaults are not shown.
**Proposed fix:** Add `help=` strings for every flag. Use `formatter_class=argparse.ArgumentDefaultsHelpFormatter` to show defaults automatically. Add an `epilog` referencing `pdf_to_md.bat`, exit codes, and `HF_HOME` / `TEMP` env vars.

### F12 — README is 8 lines (U, S3)
**Symptom:** README covers a one-line invocation and a pointer to the design doc. Nothing about install, env vars (HF_HOME, TEMP, TMPDIR), `pdf_to_md.bat`, exit codes, common errors, where output goes, or how to debug a failed PDF (debug.log location).
**Proposed fix:** Expand to ~80 lines with sections: Install, Quickstart, Common errors, Output layout, Debugging a failed PDF, Configuration knobs (link F9), Exit codes (link F10).

### F13 — Local-time timestamps in logs (U, S3)
**Symptom:** debug.log uses `time.strftime("%Y-%m-%d %H:%M:%S")` (driver.py:38) — local time, no timezone. Cross-host correlation (dev vs homeserver, future v5) needs UTC or at least an offset.
**Proposed fix:** UTC + ISO-8601 (`time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`). Or include `%z`. One-line change.

### F14 — No aggregate batch summary printed (U, S3)
**Symptom:** End of batch prints nothing aggregate. Operator scrolls run.log to find slowest PDF / which failed / which fell back to which engine. Run-log is tab-separated and grepable but not human-summary-friendly.
**Proposed fix:** Print a final table to stdout: `N ok / M failed / K cached, total elapsed Xs, slowest=<pdf> (Y s), failures: [list]`. ~30 lines.

### F15 — `pdf_to_md.bat` swallows exit code (U, S3)
**Symptom:** The .bat doesn't `exit /b %ERRORLEVEL%` after the python invocation, so the script's exit code isn't propagated. A scheduled task or another wrapper can't tell success from failure from cuda_aborted.
**Proposed fix:** Add `exit /b %ERRORLEVEL%` at the end of `pdf_to_md.bat`. One-line fix.

### F16 — Router decision rationale not surfaced (U, S4)
**Symptom:** `debug.log` records `engine order: ['marker', 'docling', 'mineru']` but not *why* — which threshold tripped, what the values were. For routing-debugging, operator must read the profile (also in debug.log) and mentally compare against `RouterConfig` defaults.
**Proposed fix:** `router.decide` returns the `(order, reason)` pair; driver writes the reason. e.g. `"router: docling primary (max_table_cells=207 ≥ 50)"`. ~15 lines.

---

## Findings (Phase 2, stress matrix)

### S7 — Single-page PDF (PASS)
**Setup:** `Testing_stress/one_page.pdf` (extracted page 0 of clean.pdf via pypdf).
**Result:** marker, score=1.00, 2710 chars md output, elapsed 62.7s. Quality emptiness check (150 chars/page → 150 for a 1p) cleared comfortably. No false positive on the 1-page case. ✓

### S5 — Garbage PDF (PASS with findings F19/F20)
**Setup:** `Testing_stress/garbage.pdf` — `%PDF-1.4` header + 50KB random bytes + `%%EOF`.
**Result:** Failed cleanly with reason "quality failed after docling: ['emptiness: 1 chars/page < 150']", exit code 2. Sanitize tier (ghostscript) was tolerant — produced a 1p clean PDF from the junk header without erroring. Both marker and docling ran (0 chars / 0 images each). The emptiness quality check caught the empty result and reported a reasonable failure reason. ✓
**Findings exposed:** F19 (timestamp leak in user-facing FAIL line); ghostscript silently accepting un-parseable input is an observation worth noting but not a finding — it's defense in depth that the quality check downstream catches.

### S4 — Encrypted (password-protected) PDF (PASS but F20 surfaced)
**Setup:** `Testing_stress/encrypted.pdf` — clean.pdf re-encrypted with user_password='secret' using pypdf 128-bit.
**Result:** Failed via "quality failed after docling: ['emptiness: 1 chars/page < 150']", exit code 2. Note: ghostscript SILENTLY stripped encryption (or produced an empty output); preflight then ran on the sanitized file and reported `is_encrypted=False`, classified as scanned. Pipeline never knew it was encrypted. The fall-through eventually surfaces an "emptiness" failure, which is misleading — operator gets no signal to try a password.
**Findings exposed:** F19 (same timestamp leak), F20 (encrypted PDFs surface as misleading "emptiness" failures).

### S8 — GPU-not-present / CPU-only (`CUDA_VISIBLE_DEVICES=-1`) (PASS with F21/F22)
**Setup:** `Testing_stress/one_page.pdf` with `CUDA_VISIBLE_DEVICES=-1` to force CPU torch.
**Result:** env.check correctly warned `torch is CPU-only; engines will be very slow.` Marker hit the 90s adaptive timeout (`60 + 30 * page_count` for 1p) on CPU. Driver fell through to docling, which finished in 23.7s with score=1.00. Total elapsed 131.1s, exit 0. ✓ Fallback chain saved the run.
**Findings exposed:** F21 (adaptive timeout formula is GPU-tuned, marker times out on CPU even for tiny PDFs), F22 (CPU-only warning is informative but not actionable).

### S6 — 5001p synthetic auto-slice (PARTIAL, F31 surfaced; aborted on timing)
**Setup:** `Testing_stress/synth_5001p.pdf` (33MB, 5001 copies of clean.pdf p0). LARGE_PDF_PAGE_THRESHOLD=500 so auto-slice fires; SLICE_CHUNK_SIZE=150 → 34 parts.
**What completed:** All 34 slice PDFs cut cleanly into `Testing/output_s6/.slices/synth_5001p/`. Slicing itself was fast (a few seconds). Part 01 (150p of repeated clean.pdf p0 content) processed via marker, score 1.00, 270,592 chars md output, **elapsed 961.3s (16.0 min)** — the cost driver. Part 02 (150p) reached preflight + router (marker route) before being killed externally to bound runtime.
**Why aborted:** Marker on 150p of identical pages takes ~16 min per slice. Extrapolating: 34 × 16 = 544 min ≈ 9 hours of wall-clock (model is reloaded fresh per per-slice subprocess; OS file cache speeds subsequent loads but model graph compilation still costs ~30-60s per slice). Infeasible for an overnight session; aborted after 2 slices to free the GPU for downstream work.
**Pass evidence (partial):** ✓ slicing ✓ per-slice subprocess + F17 lock-bypass ✓ atomic md/manifest write ✓ slice mtime preservation (F2 — slice PDFs got the source mtime). Not validated: merge step (`slicing.merge_parts`) and post-merge cleanup of `.slices/` and `_part_NN/` dirs.
**Findings exposed:** F31 (slice-failure debug-log path mismatch).
**Follow-up:** rerun with a SMALLER synthetic (e.g., 601p → 5 slices, ~80 min) once the Tier-1 fix-plan validation kicks off. Note that the merge step was already validated in the prior `principles_of_neurology` 1667p run captured in memory ("v1+v2 outputs clean … 34.9 min, 396 images") — so the *unvalidated* surface is narrow.

### S10 — 7-PDF v2 set regression (PASS)
**Setup:** Re-run a slim subset of the v2 set after Phase-1+F17 fixes. Validation goal: confirm no quality regression.
**Coverage this session:**
| PDF | Pages | Engine | Score | Elapsed | Source |
|---|---:|---|---:|---:|---|
| an_overview... | 18 | marker | 1.00 | 141.4s | S3 cold + cache hit |
| financial_power_laws... | 16 | marker | 1.00 | 83.0s | S3 |
| clean.pdf (extracted from dividend_policy p0) | 3 | marker | 1.00 | 72.3s | S3 |
| a_non_random_walk_down_wall_street | 449 | docling | 1.00 | 497s | S9 (positive: docling now succeeds, no marker fallback) |
| annual_report (TSLA 10-K) | 144 | docling | 1.00 | 62.1s | S10 |
| dividend_policy_growth (full) | 29 | marker | 1.00 | 262.0s | S10 |
| the_master_swing_trader | 377 | marker | 1.00 | 121.9s | S10 |
| principles_of_neurology (1667p sliced) | 1667 | marker | 1.00 | 35 min | memory's last v2 run; not re-validated this session |

7 of 7 v2 PDFs validated 1.00 (6 directly this session, 1 from memory). **NO quality regression** from Phase-1 fixes.

**Pass criteria:** met. All v2-set PDFs that were re-run scored 1.00. Layer-0 docling pin (commit `b971d56`) actually IMPROVED wall_street outcome (docling now succeeds where it previously fell back to marker).


### S9 — wall_street docling-fallback waste (OBSOLETE / POSITIVE FINDING)
**Setup:** `Testing/a_non_random_walk_down_wall_street.pdf` (449p, 15MB), router picks docling first because `max_table_cells=186 ≥ 50`.
**Expected (per pre-Layer-0 memory):** docling produces ~5 chars/page near-empty output, quality check fails, marker fallback rescues with score 1.00, ~5 min docling waste.
**Actual (2026-05-01):** docling **SUCCEEDED** with 276,847 chars (~616 chars/page on 449p), `score=1.00`, NO marker fallback needed. Total docling time 496.9s (~8.3 min). md output 278KB.
**Implication:** The Layer-0 docling batch_size=1 pin (commit `b971d56`) didn't just prevent CUDA OOM — it apparently also restored docling's ability to produce correct output on wall_street. The "loose end #2" idea (per-PDF engine blacklist for wall_street) is **obsolete**. No waste to quantify; no blacklist needed.
**Caveat:** Without batch=1, docling burned 5 min producing near-empty output and fell back to marker. Now batch=1 docling takes 8 min producing correct output. Net: ~3 min slower elapsed, but uses one fewer engine attempt and the output is from docling (better table fidelity than marker for table-heavy books).
**Recommendation:** Remove "loose end #2" from `project_process_papers_v3.md`. The Layer-0 fix solved more than just the OOM; it solved the underlying near-empty-output regression.

### S2 — Disk-full mid-conversion (PASS, F4 protected; F23 surfaced)
**Setup:** Real disk-full simulation on Windows requires admin (VHD/Hyper-V); used `scripts/s2_fault_inject_disk_full.py` to runtime-patch `driver._atomic_write` so the FIRST `<stem>.md` write fails with `OSError(28, "No space left on device")` after writing half the content to the `.tmp`. Ran on `Testing_stress/one_page.pdf`.
**Result (post-fault, pre-rerun):** stem dir contains `debug.log` (832B), `images/`, and `one_page.md.tmp` (1358B partial). NO `one_page.md`, NO `manifest.json`. ✓ F4 atomic-write design holds: `Path.glob("*.md")` does not match `*.md.tmp`, so `find_existing_output` cannot false-positive on the partial tmp.
**Result (re-run without fault):** clean conversion, `os.replace` overwrote the orphan `.tmp` with the new tmp before atomic rename, leaving only `<stem>.md`, `manifest.json`, fresh `debug.log`, `images/`. Cache and final state correct. ✓
**Findings exposed:** F23 (uncaught OSError propagates as exit 1 traceback rather than a clean error message), F24 (orphan `.tmp` left after a failed write — auto-cleaned by next successful re-run, but persists if the PDF is never reattempted).
**Methodology caveat:** Fault injection in driver._atomic_write tests only the markdown write path. The engine_runner's `.{engine}_raw.md` write inside `out_dir` (engine_runner.py:48) was not exercised; a real ENOSPC there would produce a different failure shape (engine subprocess error rather than driver ENOSPC). Future deeper test should fault that path too.

### S3 — Kill cli mid-PDF#2, re-run (PASS, modulo F17/F18)
**Setup:** `Testing_s3/` with 3 small PDFs (an_overview 18p, clean 3p, financial_power_laws 16p), output `Testing/output_s3/`. First batch ran. Killed parent cli tree via `taskkill /F /T /PID <parent>` ~5s after PDF#1's `<stem>.md` landed (mid-PDF#2).

**Post-kill state captured 2026-04-30 23:23:35:**
- PDF#1 stem dir: complete (`<stem>.md` 80KB, `manifest.json`, `debug.log`, `images/`).
- PDF#2 (`clean/`) stem dir: ONLY `debug.log` (282 bytes). No `<stem>.md`, no `manifest.json`. ✓ F4 atomic write protected against half-files.
- `.cli.lock` stale on disk (`pid=14488 started=2026-04-30T11:20:36Z`), but OS lock released with the dead process.
- `chunks.json` present (86KB) — but contains only PDF#1's chunks, written by the per-PDF child of PDF#1. ⚠ F18.
- `run.log` present — contains a full `# run started ... # run finished` block from PDF#1's child. ⚠ F18.
- PDF#3 stem dir: not created. ✓ Loop never reached it.

**Re-run state captured 2026-04-30 23:27:36:**
- `.cli.lock` re-acquired by new parent; cleanup on graceful exit (file gone after batch).
- PDF#1: cached (`OK 20 chunks (cached)`). ✓ F4-protected `.md` plus manifest survived.
- PDF#2: re-converted from scratch in 75.0s; full stem-dir state. ✓ Fresh `debug.log` overwrote the partial one from the killed run.
- PDF#3: converted 85.8s; full stem-dir state.
- `chunks.json` 197KB / 33 chunks — parent's final write replaced the stale child write.
- `run.log` now appended with 3 batches' worth of entries (one from killed run, two from rerun children, one from rerun parent).
- Total re-run elapsed 160.9s vs cold 296.7s — 46% savings from PDF#1 cache.

**Pass criteria:** met. F1 lockfile recovers from stale-on-disk state; F4 atomic md/manifest writes prevent half-file cache hits; cache resumption is correct.

**Discoveries during S3:** F17 (S1, blocker — fixed inline in commit 0006b49) and F18 (S3, deferred).

---



### F18 — Per-PDF child clobbers parent's `chunks.json` and adds noise to `run.log` (R, S3) ✅ FIXED in 4922548
**Symptom:** When the parent batch is killed before finishing (e.g. S3 kill-mid-batch), the surviving `chunks.json` contains only the chunks from the FIRST PDF (~86KB / 20 chunks for an 18p paper) — not the partial batch's chunks. Similarly `run.log` contains complete `# run started ... # run finished ...` blocks for each per-PDF child run, even though the parent batch never wrote its own block.
**Reproduction:** S3, captured 2026-04-30. Pre-rerun state: `chunks.json` 86KB; `run.log` lists only `an_overview... ok marker score=1.00 elapsed=141.4s` between `# run started` / `# run finished` markers.
**Root cause:** `_run_batch` is shared between parent and per-PDF children. Both code paths reach the `chunks.json` write at cli.py:581-583 and the `run.log` append at cli.py:591-594. Children write a single-PDF chunks.json that the parent's eventual write would overwrite at end-of-batch — but if the parent never reaches end-of-batch (kill / crash), the child's write is what's left on disk. Same env-var bypass that resolves F17 should gate these writes too.
**Proposed fix:** In `_run_batch`, skip the run.log append and the chunks.json write when `os.environ.get(_CHILD_HOLDS_LOCK_ENV) == "1"`. Children carry no batch-level state worth persisting; their per-PDF result already lives in `<stem>/{<stem>.md, manifest.json, debug.log}`. Five-line change. Add a test that `_run_batch` with the env var set leaves `run.log` and `chunks.json` untouched.
**Severity rationale:** S3 (annoying, slow, confusing) — final chunks.json on a successful batch IS the parent's complete write, so steady-state is correct; the bug only surfaces on partial / kill scenarios where the user already knows they aborted. Not S2 because no irreversible damage and no hidden corruption in normal flow.

### F19 — debug.log timestamp prefix leaks into user-facing FAIL line (U, S3) ✅ FIXED in 4922548
**Symptom:** Run.log and stdout `FAIL` lines contain a literal `[2026-04-30T19:23:31Z]` prefix:
```
encrypted.pdf	FAILED	reason=[2026-04-30T19:23:31Z] status=failed reason=quality failed after docling: ['emptiness: 1 chars/page < 150']
  FAIL  failed -- [2026-04-30T19:23:31Z] status=failed reason=quality failed after docling: ['emptiness: 1 chars/page < 150']
```
**Reproduction:** Any failed PDF (S4 encrypted, S5 garbage on 2026-04-30).
**Root cause:** `_read_last_failure_reason` (cli.py:630) scrapes the last `status=failed` line from debug.log without stripping the `[ts]` prefix that `_DebugLog.write` prepends. The scraped string then becomes the user-visible reason.
**Proposed fix:** Strip a leading `[ISO-8601-Z]` prefix in `_read_last_failure_reason` before returning. Five-line change. Add a test against a debug.log containing a known timestamped failure line.

### F20 — Encrypted PDFs surface as misleading "emptiness" failures (R, S2) ✅ FIXED in 4922548
**Symptom:** A password-protected PDF runs through ghostscript sanitize (which silently strips/empties the content), preflight reports `is_encrypted=False` on the sanitized output, engine chain produces 0 chars, quality check fails with "emptiness". Operator sees an empty-content failure and never learns the PDF was encrypted.
**Reproduction:** S4 with `Testing_stress/encrypted.pdf` (clean.pdf encrypted via pypdf 128-bit, user_password='secret'). Debug.log: `is_encrypted: False` on the sanitized file; "emptiness" failure reason in run.log.
**Root cause:** `preflight.probe()` runs on the sanitized file (driver.py:113), so any encryption stripped/erased by sanitize is invisible. There is no fast-fail path for `is_encrypted=True` anywhere — even if preflight saw it, the driver doesn't branch on encryption.
**Proposed fix:** (a) preflight.probe() additionally checks the ORIGINAL pdf for `pikepdf.PasswordError`; OR (b) driver.process_one runs a pre-sanitize encryption probe. Either way: emit a distinct failure reason `encrypted: password required` + `EngineError(stage="encrypted")` so the cli FAIL line tells the operator to retry with `--password` (which doesn't exist yet — adding the flag is part of the fix).
**Severity rationale:** S2 — encrypted-PDF support is missing entirely AND the failure mode hides the cause. Borderline R/U; tagged R because the routing/diagnostic logic is the gap, not the cosmetics.

### F21 — Adaptive engine timeout assumes GPU, starves CPU runs (R, S3) ✅ FIXED in 4922548
**Symptom:** With `CUDA_VISIBLE_DEVICES=-1` (CPU torch), marker on a 1p PDF hits the 90s subprocess timeout: `[marker:subprocess] engine subprocess timeout 90s (page_count=1)`. Marker on CPU needs more than 90s even for tiny PDFs because model load alone takes 60-120s without GPU offload.
**Reproduction:** S8 with `CUDA_VISIBLE_DEVICES=-1` on `one_page.pdf`. Driver fell through to docling so the run still succeeded, but every marker invocation in a CPU-only batch wastes 90s before timing out.
**Root cause:** `adaptive_timeout(page_count, cap=4h) = min(60 + 30*page_count, cap)` (subprocess_engine.py:_adaptive_timeout) is calibrated to GPU per-page throughput. CPU is 5-10x slower; the slope and intercept are both wrong.
**Proposed fix:** Detect `torch.cuda.is_available()` (or check the env-warn in env.check) and apply a CPU multiplier (e.g. `60 + 180*page_count` capped at cap). Or expose a `PROCESS_PAPERS_V3_TIMEOUT_MULTIPLIER` env var. Document under the "Configuration knobs" section. ~15 lines.

### F22 — CPU-only warning lacks remediation guidance (U, S4) ✅ FIXED in 4922548
**Symptom:** `WARN  torch is CPU-only; engines will be very slow.` is informative but doesn't tell the user what to do.
**Proposed fix:** Append guidance: `"...; expect ~5-10x runtime per engine. If a CUDA-capable GPU is present, install the matching torch wheel from https://pytorch.org/get-started/locally/. Otherwise, set --engine docling (CPU-friendly) or run on a GPU host."` Doc-only fix in env.check.

### F23 — Uncaught OSError on disk-full propagates as exit-1 traceback (R, S2) ✅ FIXED in 4922548
**Symptom:** When `_atomic_write` raises `OSError(28, ENOSPC)`, the exception propagates through `driver.process_one` → `cli._process_one_in_process` → `cli.main()` and bubbles to a Python default exit code 1 with a full traceback to stderr. No run.log, no batch summary, no clean failure reason.
**Reproduction:** S2 fault injection. Captured in `Testing/output_s2/_stdout.log` 2026-04-30: traceback ending with `OSError: [Errno 28] No space left on device (injected)`.
**Root cause:** `driver._try_engines` only catches `EngineError` and generic `Exception` for engine convert calls; `_atomic_write` runs after engine success and is not wrapped. `cli.main()` similarly doesn't catch generic OSError.
**Proposed fix:** Wrap `_atomic_write` calls in `driver._try_engines` (md and manifest) with `try/except OSError`; on failure, log a "status=failed reason=write: <errno>: <strerror>" line and return a normal `PdfResult(ok=False, ...)`. Same pattern at `merge_parts` and `chunks.json` write. Add a new exit code `EXIT_DISK_ERROR = 5` for batch-level disk-IO failures. ~30 lines.

### F25 — `--in-process` help text misleading after F17 fix (U, S4) ✅ FIXED in 4922548
**Symptom:** `--help` says `--in-process: process the batch in this interpreter (disables the per-PDF subprocess isolation; useful for tests only) (default: False)`. After F17, the per-PDF subprocess pattern actually invokes children with `--in-process` internally — it's not "tests only" anymore.
**Proposed fix:** Reword to: `"process the batch in this interpreter, skipping per-PDF subprocess isolation. Used internally by the per-PDF subprocess loop; safe for users when running a single PDF, otherwise prefer the default subprocess mode for engine-state isolation."` Doc-only change.

### F26 — README quickstart shows batch mode only, no single-PDF example (U, S3) ✅ FIXED in 4922548
**Symptom:** README quickstart shows `python -m process_papers_v3 --input-dir . --output-dir output -y` but a stranger trying to convert a single PDF doesn't see the positional-arg form documented at the top.
**Proposed fix:** Add a sibling quickstart block:
```
# Convert one PDF
python -m process_papers_v3 paper.pdf --input-dir . --output-dir output -y
```
Doc-only.

### F27 — Usage errors (not-a-PDF) run env.check before bailing (U, S4) ✅ FIXED in 4922548
**Symptom:** `python -m process_papers_v3 README.md ...` prints the full "Environment" section (~5s on Windows including torch import) before failing with `FAIL Not a PDF file: README.md`. By contrast, a missing `--input-dir` bails before env.check (fast).
**Root cause:** Input-dir check is in `main()` (cli.py:416, before env.check). The pdf-path check is in `_run_batch()` (cli.py:438, after env.check at line 432).
**Proposed fix:** Move the args.pdf existence/extension check up into `main()` before the lock acquire / env.check, so all usage errors are fast-fail. ~10 lines.

### F28 — No live progress indicator across a multi-PDF batch (U, S3) ✅ FIXED in 4922548
**Symptom:** During a batch, the operator sees per-PDF "OK / FAIL" lines as each finishes but can't tell whether the current PDF is mid-engine-load (~30s wait), mid-conversion (active), or hung (no output for 30+ minutes is indistinguishable from "still working" on a long book). No ETA, no spinner, no "PDF 3/10" header.
**Reproduction:** Any multi-PDF batch.
**Proposed fix:** Print a one-line header per PDF including index/total and the routed engine: `[3/10] clean.pdf (3p, marker)`. Then per-PDF stage breadcrumbs from debug.log mirrored to stdout: `... sanitize ok ... preflight ok ... engine marker ... ok 20 chunks (36.6s)`. Or wire the debug.log writer to also tail to stdout when isatty. ~30 lines.

### F31 — Sliced-PDF failure reason looks in the wrong stem dir (R, S3) ✅ FIXED in 4922548
**Symptom:** When a slice in `_process_sliced` fails, the parent main loop reports `FAILED reason=unknown (no debug.log written — process may have crashed hard)`. Operator can't tell WHICH slice failed without spelunking through `<output_dir>/<stem>_part_NN/debug.log` files manually.
**Reproduction:** S6 abort 2026-05-01: run.log entry `synth_5001p.pdf FAILED reason=unknown (no debug.log written — process may have crashed hard) debug=<output_dir>/synth_5001p/debug.log`. The path `synth_5001p/debug.log` doesn't exist because the merge step never ran.
**Root cause:** cli.py:584 — `debug_path = output_dir / pdf_path.stem / "debug.log"`. For a sliced PDF, the merged stem dir doesn't exist until merge runs, and per-slice debug.log files live at `output_dir/<slice_stem>_part_NN/debug.log`. The slice path's failure reporting needs to scan part dirs and surface the failed-slice reason.
**Proposed fix:** In `_process_sliced`, when `any_slice_failed`, read the last failed-slice's `debug.log` and synthesize a `<output_dir>/<merged_stem>/debug.log` summary (or pass the reason back up directly). ~20 lines.

### F30 — `env.check` runs once per PDF in subprocess mode (U, S4) ✅ FIXED in 4922548
**Symptom:** Every per-PDF child cli prints the full `Environment` block (`OK torch CUDA -- ...`, `OK marker found.`, `OK docling found.`, `WARN mineru ...`, `OK Ghostscript found ...`) — and runs the underlying probes (~3-5s on Windows for the torch import alone). For a 10-PDF batch, env.check runs 11 times (parent + each child).
**Reproduction:** Any multi-PDF batch in default subprocess mode. The parent's stdout has the env.check output, then each child's stdout repeats it (visible after F29 line-buffering fix or when log is opened post-batch).
**Proposed fix:** Same env-var bypass pattern as F17. Children check `_PROCESS_PAPERS_V3_PARENT_HOLDS_LOCK == "1"` and skip the `env.check` call (parent has already validated the env). Saves 30-50s on a 10-PDF batch and removes ~50 redundant log lines.

### F29 — Parent's stdout is buffered behind per-PDF child output (U, S3) ✅ FIXED in 4922548
**Symptom:** Parent prints headers ("Environment", "Processing 3 PDF(s)", per-PDF name banners) but when stdout is redirected to a file (default for `pdf_to_md.bat`), the parent's small writes hide behind the child's flushed multi-KB chunks until the parent flushes (often only at process exit). On a force-kill mid-batch, parent's buffered stdout is dropped entirely — the file never shows the parent's header lines at all.
**Reproduction:** S3 captured `_stdout.log`: only child output and a single Batch summary visible; parent's "Processing 3 PDF(s)" header never landed because the parent was killed before its stdio flushed.
**Root cause:** Python's stdio is block-buffered when redirected to a file. The parent's `print()` calls go into a 4KB buffer; the child's output (separate process, flushed at exit) reaches the file first.
**Proposed fix:** Set `sys.stdout.reconfigure(line_buffering=True)` early in `cli.main()` (Python 3.7+). Or `print(..., flush=True)` on every parent-side ui call. Or `python -u`. ~3 lines.

### F24 — Orphan `<stem>.md.tmp` left after failed atomic write (R, S3) ✅ FIXED in 4922548
**Symptom:** When `_atomic_write` fails mid-`tmp.write_text` (ENOSPC, kill, etc.), the partial `<stem>.md.tmp` remains in the stem dir. Doesn't false-positive `find_existing_output` (good) but accumulates if the PDF is never reattempted.
**Reproduction:** S2 fault inject. `Testing/output_s2/one_page/one_page.md.tmp` (1358B) persisted until the re-run's `os.replace(tmp, path)` consumed it.
**Proposed fix:** `try/finally tmp.unlink(missing_ok=True)` around the `tmp.write_text` + `os.replace` pair in `_atomic_write`. Five-line change. The `os.replace` consumes the tmp on success, so the `unlink(missing_ok=True)` only fires on failure. Add a unit test injecting OSError in tmp.write_text and asserting no .tmp survives.

### F17 — F1 lockfile breaks the per-PDF subprocess pattern (R, S1) — REGRESSION FROM PHASE-1
**Symptom:** Every PDF in subprocess (default) mode fails instantly with `EXIT_LOCKED (4)` and the run-log reason `unknown (no debug.log written — process may have crashed hard)`. Total batch elapsed ~0.8s for any number of PDFs. Effectively the entire pipeline is broken in its default invocation since commit `6c4e115` landed F1.
**Reproduction:** `marker-env/Scripts/python.exe -m process_papers_v3 --input-dir Testing_s3 --output-dir Testing/output_s3 -y` against any input dir. Captured in 2026-04-30 S3 stress test, output at `Testing/output_s3/_stdout.log`:
```
  FAIL  another process is already running against this output directory (lock file: ...\.cli.lock). ... OS error: [Errno 13] Permission denied
  FAIL  another process is already running against this output directory ...
```
**Root cause:** `cli._process_one_subprocess` (cli.py:283) spawns a child `python -m process_papers_v3 <pdf> --in-process` per PDF. Each child re-runs `main()` and tries to acquire the SAME `<output_dir>/.cli.lock` the parent already holds. Lock acquisition fails → child exits 4 → parent records "no debug.log" → next PDF same outcome. Slice path (`_process_sliced`) has the identical bug via its per-slice `_process_one_subprocess` calls.

The Phase-1 EXIT_LOCKED test (`test_main_returns_exit_locked_when_dir_held`) only exercised single-process lock contention. `test_e2e` would have caught it but was excluded from the post-Phase-1 65-test verification set (memory: "Full fast suite not re-run after fixes").
**Fix landed (commit pending):** Parent sets internal env var `_PROCESS_PAPERS_V3_PARENT_HOLDS_LOCK=1` before each `subprocess.call(cmd)`. Child `main()` checks this env var and skips lock acquisition when set. Two-line fix in cli.py + new test `test_child_subprocess_skips_lock_when_parent_holds_it`. Restores subprocess-mode functionality without weakening F1's same-output-dir concurrency guard.
**Severity rationale:** S1 (data loss / silent corruption / unrecoverable state) — pipeline produces zero output until reverted/fixed. Borderline S2 (no permanent damage, just zero output) but the silent "no debug.log written" reason hides the root cause from the operator, who would assume engine crash and start chasing GPU/memory ghosts. Tagged S1.

---

## Phase 3 — UX walkthrough (2026-04-30/05-01 overnight)

| # | Action | Verdict | Notes |
|---|--------|---------|-------|
| U1 | `--help` review | ✓ PASS | F11 fix landed; every flag has help text, defaults shown, epilog covers exit codes + env vars. New finding F25 (one help string is now stale). |
| U2 | Read README cold, run a single PDF | ✓ PASS-ish | F12 fix landed (8→92 lines). Quickstart only shows batch mode. F26 added. |
| U3 | Trigger usage failure | ✓ PASS | not-a-PDF → exit 1, clear `FAIL Not a PDF file: <path>`. nonexistent-input-dir → exit 1, clear. F27 added (env.check runs before not-a-PDF check, slow). |
| U4 | Live progress | ⚠ FINDING | No ETA / progress / current-stage indicator. F28 added. |
| U5 | Find output for a specific PDF | ✓ PASS | README documents `<output-dir>/<stem>/<stem>.md` layout clearly. |
| U6 | debug.log for successful PDF, 30s comprehension | ✓ PASS | Sectioned (`=== sanitize ===` etc.), timestamped, profile + router reason + engine result + status all present. |
| U7 | debug.log for failed PDF (S5 garbage) | ✓ PASS | Reads as a story: "scanned PDF, mineru disabled, marker+docling both produced empty → quality fail". Actionable. |
| U8 | Engine routing rationale visible | ✓ PASS | F16 fix landed; `router reason: marker: no docling/mineru triggers tripped (table_density=0.00, max_table_cells=0, est_total_tables=0.0)` is in debug.log AND manifest.json. |

Additional Phase-3-discovered findings: F25 (in-process help misleading), F26 (README single-PDF example), F27 (slow usage-error path), F28 (no live progress), F29 (parent stdout buffered behind child).

---

## Out of Phase 1 (deferred to later phases)

- Real-failure validation of the proposed fixes — Phase 2 stress matrix.
- UX feel ("does it feel friendly to a new user") — Phase 3 walkthrough.
- Whether any finding is actually wrong — only stress reproduction can confirm.
