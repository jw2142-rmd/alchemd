# PDF Pipeline Evaluation Plan

**Date:** 2026-04-30
**Scope:** `process_papers_v3` end-to-end. Goal is to identify robustness and user-friendliness gaps. Output is a triaged findings list plus a separate follow-up implementation plan — *this* plan ends at the findings doc, not at code changes.

**Why now:** Layer 0–4 CUDA protections + auto-slice + auto-router are stable; General Investment 40/40 ran clean. The pipeline is feature-complete enough that the next round of improvements should be driven by empirical weaknesses, not speculative work.

**Methodology (3 phases):**
1. **Static audit** — read-only review of code paths, error messages, configs, docs.
2. **Stress matrix** — controlled adversarial inputs and resource conditions.
3. **UX walkthrough** — first-run-as-a-stranger pass through the CLI, recording every friction point.

Each phase produces notes in `docs/superpowers/specs/2026-04-30-pipeline-eval-findings.md` (created during Phase 1). Each finding is tagged `R` (robustness) or `U` (UX), with a severity (S1 = data loss / silent corruption, S2 = batch-aborting, S3 = annoying, S4 = nice-to-have).

---

## Phase 1 — Static audit (read-only, ~2 hours)

| # | Area | What to check | Where to look |
|---|------|---------------|---------------|
| A1 | Error messages | Every `EngineError`, `ui.err`, `raise` site: is the message actionable? Does it say *what to do next*, not just *what failed*? | `engines/`, `driver.py`, `cli.py`, `sanitize.py` |
| A2 | Exit codes | Are 0/1/2/3 documented for callers? Any path that exits non-zero without a code constant? | `cli.py:main` + `pdf_to_md.bat` |
| A3 | Config surface | What's tunable, what's hard-coded, what *should* be tunable? Constants like `LARGE_PDF_PAGE_THRESHOLD`, `_EMPTINESS_CHARS_PER_PAGE`, `DEFAULT_TIMEOUT_SEC` | grep for module-level UPPERCASE constants |
| A4 | Idempotency / caching | Cache logic in `find_existing_output` — what happens on partial output? Does a half-merged sliced run cache correctly? | `cli.py:find_existing_output`, `slicing.py:merge_parts` |
| A5 | Logging quality | run.log + debug.log: enough to triage a failure 30 days later without re-running? Any sensitive data leaks? | `driver._DebugLog`, `cli` run-log writes |
| A6 | Help text | `python -m process_papers_v3 --help` — flags self-documenting? Defaults shown? | `cli.build_parser` |
| A7 | README / first-run docs | Does `README.md` cover install, env vars, common errors? | `README.md` |
| A8 | Concurrent-run safety | What if two `pdf_to_md.bat` instances target the same `output/`? File locking? Nope? | `cli.py`, `slicing.py` |
| A9 | Reboot recovery | After exit-3 cuda_aborted + reboot, does the next batch resume cleanly via the cache? Any half-written intermediates that confuse caching? | trace `--force` path + cache check |

## Phase 2 — Stress matrix (~4 hours, some real PDF runs)

Each row is a controlled experiment. Some need a synthetic PDF; flag the ones we'd need to fabricate.

| # | Stressor | Inputs needed | Pass criteria | Risks |
|---|----------|---------------|---------------|-------|
| S1 | Real CUDA OOM (Layer 2 + 3 live) | Disable Layer 0 (revert docling batch sizes), re-run `life_cycle_investing*.pdf` (the original PDF 17 trigger) | Layer 2 detects, batch aborts with exit 3, no marker hang | **Driver poisoning → reboot required.** Schedule for end-of-day. |
| S2 | Disk full mid-conversion | Mount a small loopback dir as `output/`, fill it during a run | No data corruption; either retries or fails with actionable message | Low — local |
| S3 | Killed parent mid-batch | Run a 3-PDF batch, kill the cli mid-second-PDF, re-run | 1st cached, 2nd half-output cleaned or re-run, 3rd fresh | Medium — may confuse cache |
| S4 | Encrypted-PDF chain | Find or generate a password-protected PDF | Preflight detects, sanitize attempts decrypt, clean fail message if not | Low |
| S5 | "Garbage" PDF | Random bytes with a `%PDF-1.4` header | All sanitize tiers fail cleanly, useful error | Low |
| S6 | Massive PDF (>5000p) | Synthetic via repeated-page concat | Auto-slices into ~33 parts, all merge cleanly | High disk + multi-hour |
| S7 | Single-page PDF | Existing `clean.pdf` (3p) is close; create a 1p | Quality check doesn't false-positive emptiness | Low |
| S8 | GPU not present | Set `CUDA_VISIBLE_DEVICES=-1`, run a small PDF | Marker falls back to CPU cleanly; clear messaging | Low |
| S9 | wall_street docling-fallback waste | Re-run `a_non_random_walk_down_wall_street.pdf` | Confirms ~5 min docling waste; quantify exact cost; informs Gap 5 (engine blacklist) | Low |
| S10 | `Testing/` re-run with Layer 0 | Run the 7-PDF v2 set with current code (Gap 3 from earlier session) | All 7 score 1.00, confirm no regression, measure speedup vs default batch sizes | Low |

## Phase 3 — UX walkthrough (~1 hour)

Pretend to be a new user. Fresh cmd window, no prior session knowledge. Time-box each step.

| # | Action | Record |
|---|--------|--------|
| U1 | `python -m alchemd --help` | Are flags self-explanatory? What's missing? |
| U2 | Read `README.md` cold. Try to run a single PDF with the instructions only. | Time-to-first-success. Any blockers? |
| U3 | Trigger a deliberate failure (point at a non-PDF) — does the error tell you what to do? | Y/N + verbatim quote |
| U4 | Run a batch, watch the live output. Is progress meaningful? ETA? | Y/N + suggested fields |
| U5 | After batch, find the output for one specific PDF without prior knowledge of the layout. | Time-to-find. Any redirection needed? |
| U6 | Open `debug.log` for a successful PDF. Can you tell what happened in 30 seconds? | Y/N |
| U7 | Open `debug.log` for a failed PDF (use S5 garbage PDF). Can you act on it without re-reading source? | Y/N |
| U8 | Look up "what does engine=docling mean for this PDF" — is the routing decision visible/explainable? | Y/N |

---

## Findings doc structure

`docs/superpowers/specs/2026-04-30-pipeline-eval-findings.md`:

```markdown
# Pipeline Eval Findings 2026-04-30

## Summary
- N robustness findings (S1: x, S2: y, S3: z, S4: w)
- M UX findings (same severity scale)

## Findings
### F1 — <short title> (R, S2)
**Symptom:** ...
**Reproduction:** ...
**Root cause:** ... (or "needs investigation")
**Proposed fix:** ... (link to follow-up plan if non-trivial)
```

## Out of scope (this plan)

- Implementing fixes — that's a follow-up plan written *after* findings are in.
- Engine algorithm changes (e.g. swapping marker for a newer model) — orthogonal.
- Adding new strategy domains (academic vs financial vs scanned). Pipeline is engine-agnostic.

## Sequencing

1. Phase 1 first — cheap and informs Phase 2 stressor design.
2. Phase 2 stressors S2–S5, S7, S8, S9, S10 in any order.
3. **S1 last in any session, scheduled when reboot is acceptable.** S6 last because of multi-hour runtime.
4. Phase 3 can happen in parallel with Phase 2.
5. Findings doc gets written as you go, not at the end.

## Done when

- All Phase 1/2/3 rows in this plan have a result (`pass`, `fail`, or a finding ID).
- `2026-04-30-pipeline-eval-findings.md` exists with at least the summary table.
- A follow-up plan `docs/superpowers/plans/<date>-pipeline-fixes.md` is drafted listing the S1/S2 findings to fix first. (Drafted, not executed — execution is its own plan run.)
