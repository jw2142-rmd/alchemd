"""Subprocess entry point: load and run a single engine in-process, then
write a JSON payload the parent can read.

Used by SubprocessEngine to keep each engine's models confined to a
dedicated python interpreter. When marker's subprocess exits, its
multi-GB model cache releases — before docling's subprocess starts —
so failed engines don't stack in RAM across a single PDF's attempts.

Invocation:
    python -m alchemd.engine_runner <engine> <pdf> <out_dir> \\
        --result-path <path.json>

Writes a JSON payload. Exit 0 on success, 2 on engine failure, 1 on
argparse/loader error (same convention as argparse). The parent reads
the payload rather than the exit code so an engine's error survives
even if the child's stderr is huge or empty.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import traceback
from pathlib import Path

from alchemd.engines.base import EngineError

ENGINE_CLASSES = {
    "marker": ("alchemd.engines.marker", "MarkerEngine"),
    "docling": ("alchemd.engines.docling", "DoclingEngine"),
    "mineru": ("alchemd.engines.mineru", "MinerUEngine"),
}


def _load_engine(name: str):
    module_name, class_name = ENGINE_CLASSES[name]
    mod = importlib.import_module(module_name)
    return getattr(mod, class_name)()


def run(engine_name: str, pdf: Path, out_dir: Path, result_path: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        engine = _load_engine(engine_name)
        result = engine.convert(pdf, out_dir)
        # Large book markdown can be multi-MB; keep it out of JSON.
        md_path = out_dir / f".{engine_name}_raw.md"
        md_path.write_text(result.markdown, encoding="utf-8")
        payload = {
            "ok": True,
            "engine": result.engine,
            "markdown_path": str(md_path),
            "images": [str(p) for p in result.images],
            "elapsed": result.elapsed,
            "notes": list(result.notes),
        }
    except EngineError as exc:
        payload = {
            "ok": False,
            "engine": engine_name,
            "error_type": "EngineError",
            "error": str(exc),
            "stage": exc.stage,
            "traceback": traceback.format_exc(),
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "engine": engine_name,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "stage": "convert",
            "traceback": traceback.format_exc(),
        }

    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return 0 if payload["ok"] else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alchemd.engine_runner")
    ap.add_argument("engine", choices=list(ENGINE_CLASSES))
    ap.add_argument("pdf")
    ap.add_argument("out_dir")
    ap.add_argument("--result-path", required=True,
                    help="JSON file where parent reads success/failure payload")
    args = ap.parse_args(argv)
    return run(args.engine, Path(args.pdf), Path(args.out_dir), Path(args.result_path))


if __name__ == "__main__":
    sys.exit(main())
