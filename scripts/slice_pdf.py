"""Slice a large PDF into fixed-page-count parts for staged marker processing.

Usage:
    python scripts/slice_pdf.py <input.pdf> --out-dir <dir> [--chunk-size 150]

Each output is named <stem>_part_NN.pdf (1-based, zero-padded). Last part gets
the remainder.

Standalone CLI wrapper around alchemd.slicing.slice_pdf — auto-slice
in cli.py uses the same library function so behavior matches end-to-end.
"""
import argparse
import pathlib
import sys

# Allow `python scripts/slice_pdf.py ...` to find the project package
# without an editable install.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alchemd.slicing import page_count, slice_pdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=pathlib.Path, help="source PDF")
    ap.add_argument("--out-dir", type=pathlib.Path, required=True)
    ap.add_argument("--chunk-size", type=int, default=150)
    args = ap.parse_args()

    if not args.input.is_file():
        print(f"input not found: {args.input}", file=sys.stderr)
        return 1

    n = page_count(args.input)
    print(f"source: {args.input.name}  pages={n}  chunk={args.chunk_size}")

    parts = slice_pdf(args.input, args.out_dir, args.chunk_size)
    for idx, p in enumerate(parts, start=1):
        start = (idx - 1) * args.chunk_size + 1
        end = min(idx * args.chunk_size, n)
        print(f"  part_{idx:02d}: pages {start}-{end} -> {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
