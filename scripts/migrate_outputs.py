#!/usr/bin/env python3
"""
One-time organizer for historical Douyin video download outputs.

Move legacy flat files under outputs/douyin_video_downloads into:
- mp4/
- json/
- txt/
"""

from __future__ import annotations

import argparse
import filecmp
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "douyin_video_downloads"
FORMAT_DIRS = {
    "mp4": "mp4",
    "json": "json",
    "txt": "txt",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate historical flat download outputs into format folders.")
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output root to organize. Default: outputs/douyin_video_downloads",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned moves without changing files.",
    )
    return parser.parse_args()


def ensure_format_dir(out_dir: Path, format_name: str) -> Path:
    target = out_dir / FORMAT_DIRS[format_name]
    target.mkdir(parents=True, exist_ok=True)
    return target


def classify_file(path: Path) -> str | None:
    name = path.name
    if name.endswith(".url.txt"):
        return "txt"
    if name.endswith(".mp4"):
        return "mp4"
    if name.endswith(".json"):
        return "json"
    return None


def same_file_content(a: Path, b: Path) -> bool:
    if not a.exists() or not b.exists():
        return False
    if a.stat().st_size != b.stat().st_size:
        return False
    return filecmp.cmp(a, b, shallow=False)


def move_or_dedupe(source: Path, destination: Path, *, dry_run: bool) -> tuple[str, Path]:
    if source.resolve() == destination.resolve():
        return ("already", destination)

    if destination.exists():
        if same_file_content(source, destination):
            if not dry_run:
                source.unlink()
            return ("deduped", destination)

        candidate = destination
        stem = destination.stem
        suffix = destination.suffix
        counter = 1
        while candidate.exists():
            candidate = destination.with_name(f"{stem}__migrated_{counter}{suffix}")
            counter += 1
        destination = candidate

    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
    return ("moved", destination)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir).expanduser().resolve()
    ensure_format_dir(out_dir, "mp4")
    ensure_format_dir(out_dir, "json")
    ensure_format_dir(out_dir, "txt")

    moved: list[dict[str, str]] = []
    deduped: list[dict[str, str]] = []
    skipped: list[str] = []

    for path in sorted(out_dir.iterdir()):
        if path.is_dir():
            continue
        format_name = classify_file(path)
        if not format_name:
            skipped.append(str(path))
            continue

        destination = out_dir / FORMAT_DIRS[format_name] / path.name
        action, final_path = move_or_dedupe(path, destination, dry_run=args.dry_run)
        record = {"source": str(path), "target": str(final_path)}
        if action == "moved":
            moved.append(record)
        elif action == "deduped":
            deduped.append(record)

    summary = {
        "out_dir": str(out_dir),
        "dry_run": bool(args.dry_run),
        "moved_count": len(moved),
        "deduped_count": len(deduped),
        "skipped_count": len(skipped),
        "moved": moved,
        "deduped": deduped,
        "skipped": skipped,
    }
    import json

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
