#!/usr/bin/env python3
"""Extract frame-grid images from downloaded 2B sample videos."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

from output_names import mirror_legacy, output_path


OUTPUT_COLUMNS = [
    "视频文件",
    "抽帧图",
    "抽帧状态",
    "时长秒",
    "帧数目标",
    "错误信息",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create grid images from 2B downloaded mp4 files."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    parser.add_argument("--glob", default="2b_*.mp4", help="Video filename glob inside run_dir.")
    parser.add_argument("--frames", type=int, default=12, help="Target frame count per video grid.")
    parser.add_argument("--columns", type=int, default=4, help="Grid columns.")
    parser.add_argument("--width", type=int, default=320, help="Width of each thumbnail.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing grid images.")
    return parser.parse_args()


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"{name}_not_found")


def probe_duration(video_path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout or "{}")
    return float((data.get("format") or {}).get("duration") or 0)


def build_grid(video_path: Path, output_path: Path, *, frames: int, columns: int, width: int) -> None:
    duration = max(probe_duration(video_path), 1.0)
    frame_count = max(frames, 1)
    grid_columns = max(columns, 1)
    grid_rows = max(math.ceil(frame_count / grid_columns), 1)
    interval = max(duration / frame_count, 0.25)
    vf = f"fps=1/{interval:.3f},scale={width}:-1,tile={grid_columns}x{grid_rows}"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-frames:v",
        "1",
        str(output_path),
    ]
    subprocess.run(command, capture_output=True, text=True, check=True)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    videos = sorted(run_dir.glob(args.glob))
    rows: list[dict[str, str]] = []

    try:
        require_binary("ffmpeg")
        require_binary("ffprobe")
    except RuntimeError as exc:
        for video_path in videos:
            rows.append(
                {
                    "视频文件": str(video_path),
                    "抽帧图": "",
                    "抽帧状态": "失败",
                    "时长秒": "",
                    "帧数目标": str(args.frames),
                    "错误信息": str(exc),
                }
            )
        out_path = output_path(run_dir, "video_frame_grids.csv")
        write_csv(out_path, rows)
        mirror_legacy(out_path, run_dir, "video_frame_grids.csv")
        print(f"wrote {out_path}")
        return 1

    for video_path in videos:
        grid_path = run_dir / f"{video_path.stem}_grid.jpg"
        if grid_path.exists() and not args.overwrite:
            try:
                duration = probe_duration(video_path)
            except Exception:  # noqa: BLE001
                duration = 0
            rows.append(
                {
                    "视频文件": str(video_path),
                    "抽帧图": str(grid_path),
                    "抽帧状态": "已存在",
                    "时长秒": f"{duration:.2f}" if duration else "",
                    "帧数目标": str(args.frames),
                    "错误信息": "",
                }
            )
            continue

        try:
            duration = probe_duration(video_path)
            build_grid(
                video_path,
                grid_path,
                frames=max(args.frames, 1),
                columns=max(args.columns, 1),
                width=max(args.width, 64),
            )
            status = "成功"
            error = ""
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            duration = 0
            status = "失败"
            error = str(exc)

        rows.append(
            {
                "视频文件": str(video_path),
                "抽帧图": str(grid_path) if grid_path.exists() else "",
                "抽帧状态": status,
                "时长秒": f"{duration:.2f}" if duration else "",
                "帧数目标": str(args.frames),
                "错误信息": error,
            }
        )

    out_path = output_path(run_dir, "video_frame_grids.csv")
    write_csv(out_path, rows)
    mirror_legacy(out_path, run_dir, "video_frame_grids.csv")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
