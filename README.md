# alchemd

**Turn any pile of PDFs into perfect markdown.**

`alchemd` is a multi-engine PDF→Markdown pipeline that auto-routes each document to the best engine (marker, docling, or mineru) based on a preflight profile, auto-slices large books into 150-page parts, and ships with battle-tested safeguards against CUDA poisoning, disk-full failures, and partial-write cache corruption.

It's designed to be pointed at a directory of PDFs and run unattended overnight: idempotent, resumable, structured logs, distinct exit codes for the categories of failure that need different operator responses.

## Highlights

- **Auto-routing per PDF.** A preflight probe measures table density, max table size, and whether the document is scanned, then picks marker (prose), docling (table-heavy), or mineru (scanned) without manual hints.
- **Auto-slicing.** PDFs over 500 pages slice into 150-page parts, each routed independently, then merge cleanly into one `<stem>.md`.
- **Atomic writes.** `<stem>.md` and `manifest.json` are written via `.tmp` + `os.replace` so a power loss / kill-9 mid-write can't leave a half-file that future runs cache.
- **CUDA-poisoning protections** in five layers (root-cause prevention via batch-size pinning, allocator config, classifier-on-error, GPU health probe between PDFs, adaptive timeout). After the protections fire, the cli exits with a distinct code so a wrapping scheduler can stop the world rather than burn through 20 more guaranteed-failure PDFs.
- **Distinct exit codes** so a Task Scheduler / cron / systemd unit can react correctly to each failure category.
- **Structured logs.** Per-PDF `debug.log` with sections (`=== sanitize ===` / `=== preflight ===` / `=== router ===` / `=== engine: marker ===` / `=== done ===`) and a batch-level `run.log` with one tab-separated line per PDF.
- **Idempotent caching.** A successful conversion is cached by `<stem>.md.mtime > <pdf>.mtime`; re-running the same batch is a near-no-op.
- **Encryption fast-fail.** Password-protected PDFs surface as "encrypted: password required" instead of misleading downstream emptiness errors.

## Install

```
pip install alchemd
```

Or from source:

```
git clone https://github.com/jw2142-rmd/alchemd
cd alchemd
pip install -e .
```

The mineru engine is optional (only used for scanned PDFs):

```
pip install "alchemd[mineru]"
```

You'll also need [Ghostscript](https://www.ghostscript.com/) on `PATH` for the PDF sanitize tier.

## Quickstart

Convert every PDF in a directory:

```
python -m alchemd --input-dir ./papers --output-dir ./out -y
```

Or with the installed entry-point:

```
alchemd --input-dir ./papers --output-dir ./out -y
```

Convert a single PDF (positional name resolves against `--input-dir`):

```
alchemd paper.pdf --input-dir ./papers --output-dir ./out -y
```

For unattended Windows batches, copy `alchemd.bat.template` → `alchemd.bat`, edit the paths inside, and run it. The template moves `TEMP`, `TMPDIR`, and `HF_HOME` off the C: drive (the engines' scratch + model weights routinely run multi-GB).

## Output layout

```
<output-dir>/
    run.log                    # one tab-separated line per PDF (status, engine, score, elapsed)
    chunks.json                # all converted PDFs, chunked for downstream RAG / search use
    .cli.lock                  # advisory output-dir lock; auto-removed on graceful exit
    <stem>/
        <stem>.md              # the converted markdown
        manifest.json          # engine, quality score, profile, router reason
        debug.log              # per-PDF event log (UTC ISO-8601 timestamps)
        images/                # extracted figures
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All PDFs converted (or cached) |
| 1 | Usage error (bad args, missing input dir, not-a-PDF target) |
| 2 | Batch finished, ≥1 PDF failed for engine/content reasons — see `run.log` |
| 3 | **CUDA driver poisoned, reboot required.** Every subsequent GPU op on this host will hang. |
| 4 | Another `alchemd` instance holds the output-dir lock |
| 5 | Disk error (ENOSPC, permission) on at least one PDF — free space and retry |

A scheduler/wrapper should treat **3 specifically** as a host-level alarm. After a `cuda_aborted` exit: reboot, then re-run the same command — successful PDFs are cached and skipped.

## Environment variables

| Var | Purpose | Recommended |
|---|---|---|
| `HF_HOME` | HuggingFace model cache (3–10 GB across marker/docling/mineru) | Off the system drive |
| `TEMP` / `TMP` / `TMPDIR` | Surya/marker scratch; multi-GB on large books | Off the system drive |
| `PYTORCH_CUDA_ALLOC_CONF` | CUDA fragmentation OOM mitigation | `expandable_segments:True` (set automatically by the subprocess engine wrapper) |

## Common errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `mineru CLI not found on PATH` | `pip install mineru` didn't expose the console script | Activate the venv where `mineru.exe` lives, or `pip install "alchemd[mineru]"` |
| `ghostscript not found on PATH` | Sanitize tier needs `gswin64c` / `gs` / `gswin32c` | `winget install ArtifexSoftware.GhostScript` / `brew install ghostscript` / `apt-get install ghostscript` |
| `quality failed after marker: ['emptiness: ...']` | Marker produced near-empty output (often a scanned PDF) | Pipeline auto-falls-back to docling/mineru; if all fail, force `--engine mineru` |
| `cuda_poisoned: GPU CUDA context poisoned` | Driver in dead state | Reboot the host. The pipeline detects this and exits 3 instead of hanging. |
| `another process is already running against this output directory` | Two batches racing on the same `--output-dir` | Wait for the running batch (check `<output-dir>/.cli.lock` for the holder PID) or use a different output dir |
| `encrypted: password required` | PDF is password-protected | Decrypt before passing in (a `--password` flag is on the roadmap) |

## Debugging a failed PDF

1. Open `<output-dir>/<stem>/debug.log`. Sections are clearly labelled (`=== sanitize ===`, `=== preflight ===`, `=== router ===`, `=== engine: marker ===`, `=== done ===`).
2. The trailer line is `status=failed reason=<actionable reason>`.
3. Cross-reference with `<output-dir>/run.log` for the batch-level summary.
4. To force a different engine: `alchemd <pdf> --engine docling` (or `marker`, `mineru`).

## Configuration knobs

Tunable constants live as module-level UPPERCASE names. Edit at source if you need to deviate:

| Constant | File | Purpose |
|---|---|---|
| `LARGE_PDF_PAGE_THRESHOLD` (500) | `alchemd/cli.py` | PDFs over this auto-slice into 150-page parts |
| `SLICE_CHUNK_SIZE` (150) | `alchemd/cli.py` | Page count per auto-slice part |
| `_PER_PAGE_TIMEOUT_SEC` (30) / `_BASELINE_TIMEOUT_SEC` (60) / `_CPU_TIMEOUT_MULTIPLIER` (6) / `DEFAULT_TIMEOUT_SEC` (4 h cap) | `alchemd/engines/subprocess_engine.py` | Adaptive engine subprocess timeout |
| `_EMPTINESS_CHARS_PER_PAGE` (150) / `_GARBLED_MAX_FRACTION` (0.35) | `alchemd/quality.py` | Quality-check thresholds |
| `RouterConfig.table_density_threshold` (1.5) / `est_total_tables_threshold` (20) / `max_table_cells_threshold` (50) | `alchemd/router.py` | When to route to docling instead of marker |
| `DOCLING_LAYOUT_BATCH` / `DOCLING_OCR_BATCH` / `DOCLING_TABLE_BATCH` / `DOCLING_NUM_THREADS` (all 1) | `alchemd/engines/docling.py` | Pinned to 1 to prevent VRAM fragmentation OOMs on table-heavy PDFs |
| `PAGE_CHUNK_THRESHOLD` (300) / `PAGE_CHUNK_SIZE` (200) / `CHUNK_WORKERS` (1) | `alchemd/engines/marker.py` | Marker's internal page-range chunking |

## Architecture & history

`alchemd` carries the lineage of a long-running PDF→Markdown pipeline (formerly `process_papers_v3`) and ships with the closure of 31 evaluation findings (F1–F31) discovered in a structured static-audit + stress-matrix + UX-walkthrough cycle. Those documents live under `docs/`:

- [`docs/pipeline-evaluation.md`](docs/pipeline-evaluation.md) — the 3-phase evaluation plan
- [`docs/pipeline-eval-findings.md`](docs/pipeline-eval-findings.md) — the 31 findings, severity-tagged, all marked FIXED with commit refs
- [`docs/pipeline-fixes.md`](docs/pipeline-fixes.md) — the Tier-1/2/3 implementation plan that landed v1.0

If you want to understand why the pipeline behaves a particular way (e.g. *why* docling batch sizes are pinned to 1, *why* the lockfile bypasses for child subprocesses, *why* there are five layers of CUDA-poisoning protection), those documents are the most direct reference.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
