"""Merge per-part marker outputs (from a sliced PDF) into one extraction.

Usage:
    python scripts/merge_marker_parts.py <stem> \\
        --parts-dir Testing/output \\
        --out-dir Testing/output

Auto-discovers all <parts-dir>/<stem>_part_NN/ directories, concatenates their
markdown (with HTML comment part separators), copies images with a partNN_
filename prefix to avoid collisions, and writes an aggregate manifest.

Standalone CLI wrapper around alchemd.slicing.merge_parts — auto-slice
in cli.py uses the same library function so behavior matches end-to-end.
"""
import argparse
import json
import pathlib
import sys

# Allow `python scripts/merge_marker_parts.py ...` to find the project package
# without an editable install.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alchemd.slicing import merge_parts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stem", help='source PDF stem, e.g. "principles_of_neurology"')
    ap.add_argument(
        "--parts-dir",
        type=pathlib.Path,
        required=True,
        help="dir containing <stem>_part_NN/ subdirs",
    )
    ap.add_argument(
        "--out-dir",
        type=pathlib.Path,
        required=True,
        help="merged output written to <out-dir>/<stem>/",
    )
    args = ap.parse_args()

    try:
        final_md = merge_parts(args.stem, args.parts_dir, args.out_dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    manifest = json.loads((final_md.parent / "manifest.json").read_text(encoding="utf-8"))
    n_imgs = sum(1 for _ in (final_md.parent / "images").iterdir())
    total_elapsed = float(manifest.get("elapsed_sec_total", 0))
    n_issues = len(manifest.get("quality_issues") or [])
    print(f"merged {manifest['merged_from_parts']} parts -> {final_md}")
    print(f"  total elapsed: {total_elapsed/60:.1f} min")
    print(f"  images copied: {n_imgs}")
    print(f"  quality issues: {n_issues}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
