#!/usr/bin/env python3
"""Collect a Douyin profile's public work data, then download selected media."""

from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from output_names import existing_output_path, mirror_legacy, output_path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_OUTPUT_DIR = SKILL_ROOT / "outputs" / "douyin_profile_video_collect"


ALL_CANDIDATE_COLUMNS = [
    "建议下载",
    "作品ID",
    "作品标题",
    "抽样锚点",
    "抽样理由",
    "点赞数",
    "评论数",
    "收藏数",
    "分享数",
    "总互动",
    "是否发现挂车",
    "挂车证据",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect public Douyin profile data and download candidate/all collected media."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Douyin profile share text, profile URL, video URL, or aweme_id.",
    )
    parser.add_argument("--count", type=int, default=10, help="Target non-pinned, non-cart profile works.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output root directory.")
    parser.add_argument(
        "--resume-run-dir",
        help="Skip collection and continue from an existing timestamp run directory.",
    )
    parser.add_argument(
        "--download-mode",
        choices=["ask", "all-collected", "samples", "none"],
        default="ask",
        help="Ask before downloading, download all collected rows, selected high/low samples, or no media.",
    )
    parser.add_argument("--top-k", type=int, default=2, help="High/low samples per metric in samples mode.")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=0,
        help="Optional cap for samples mode. 0 means no cap.",
    )
    parser.add_argument(
        "--item-timeout",
        type=int,
        default=120,
        help="Maximum seconds to spend on one media item during download. Use 0 to disable.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite previously downloaded media.")
    return parser.parse_args()


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=str(SCRIPT_DIR), text=True, capture_output=True, check=False)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"command_failed:{' '.join(args)}")
    return result


def parse_collect_stdout(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"collect_stdout_not_json:{stdout[-1000:]}") from exc


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def safe_field(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def load_collected_rows(run_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    sources = [
        ("主页普通作品", "creator_posts.csv"),
        ("挂车作品", "cart_posts.csv"),
    ]
    for anchor_label, legacy_name in sources:
        path = existing_output_path(run_dir, legacy_name)
        if not path.exists():
            continue
        for row in read_csv(path):
            aweme_id = safe_field(row, "作品ID", "aweme_id", "aweme_id_str")
            if not aweme_id:
                continue
            rows.append(
                {
                    "建议下载": "是",
                    "作品ID": aweme_id,
                    "作品标题": safe_field(row, "作品标题", "标题", "desc"),
                    "抽样锚点": anchor_label,
                    "抽样理由": "本次主页采集范围内作品，按用户要求自动下载。",
                    "点赞数": safe_field(row, "点赞数", "点赞", "digg_count"),
                    "评论数": safe_field(row, "评论数", "评论", "comment_count"),
                    "收藏数": safe_field(row, "收藏数", "收藏", "collect_count"),
                    "分享数": safe_field(row, "分享数", "分享", "share_count"),
                    "总互动": safe_field(row, "总互动", "total_interaction"),
                    "是否发现挂车": safe_field(row, "是否发现挂车"),
                    "挂车证据": safe_field(row, "挂车证据"),
                }
            )
    return rows


def write_all_candidates(run_dir: Path) -> Path:
    rows = load_collected_rows(run_dir)
    path = output_path(run_dir, "video_sample_candidates.csv")
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_CANDIDATE_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    mirror_legacy(path, run_dir, "video_sample_candidates.csv")
    md_path = output_path(run_dir, "video_sample_candidates.md")
    lines = [
        "# 自动下载候选清单",
        "",
        f"- 来源目录：`{run_dir}`",
        f"- 下载模式：all-collected",
        f"- 候选数量：{len(rows)}",
        "",
        "| 作品ID | 标题 | 来源 |",
        "|---|---|---|",
    ]
    for row in rows:
        title = row["作品标题"].replace("|", "｜")
        lines.append(f"| `{row['作品ID']}` | {title} | {row['抽样锚点']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mirror_legacy(md_path, run_dir, "video_sample_candidates.md")
    return path


def shell_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def build_resume_command(run_dir: Path, mode: str, args: argparse.Namespace) -> str:
    command = [
        "python3",
        str(SCRIPT_DIR / "collect_profile_and_download.py"),
        "--resume-run-dir",
        str(run_dir),
        "--download-mode",
        mode,
    ]
    if mode == "samples":
        command.extend(["--top-k", str(max(args.top_k, 1))])
        if args.max_candidates > 0:
            command.extend(["--max-candidates", str(args.max_candidates)])
    if args.overwrite:
        command.append("--overwrite")
    if args.item_timeout != 120:
        command.extend(["--item-timeout", str(args.item_timeout)])
    return shell_command(command)


def download_options(run_dir: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    all_count = len(load_collected_rows(run_dir))
    return [
        {
            "key": "all-collected",
            "label": "下载全部采集作品",
            "description": f"下载本次进入主页普通/挂车样本池的全部作品，预计 {all_count} 条。",
            "command": build_resume_command(run_dir, "all-collected", args),
        },
        {
            "key": "samples",
            "label": "只下载高低位抽样作品",
            "description": f"按点赞/评论/收藏/分享高低位抽样下载；当前 top-k={max(args.top_k, 1)}。",
            "command": build_resume_command(run_dir, "samples", args),
        },
        {
            "key": "none",
            "label": "暂不下载",
            "description": "只保留主页采集数据和基础盘报告，不下载媒体文件。",
            "command": build_resume_command(run_dir, "none", args),
        },
    ]


def ask_download_mode(run_dir: Path, args: argparse.Namespace) -> str | None:
    options = download_options(run_dir, args)
    if not sys.stdin.isatty():
        print(
            json.dumps(
                {
                    "status": "needs_download_choice",
                    "run_dir": str(run_dir),
                    "message": "已完成主页采集，下载前需要用户选择下载方式。",
                    "options": options,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return None

    print("\n已完成主页采集。下载前请选择下一步：", file=sys.stderr)
    for index, option in enumerate(options, start=1):
        print(
            f"{index}. {option['label']} - {option['description']}",
            file=sys.stderr,
        )
    print("输入 1/2/3，或 all/samples/none：", file=sys.stderr)
    choice = input("> ").strip().lower()
    aliases = {
        "1": "all-collected",
        "all": "all-collected",
        "all-collected": "all-collected",
        "全部": "all-collected",
        "2": "samples",
        "sample": "samples",
        "samples": "samples",
        "抽样": "samples",
        "3": "none",
        "no": "none",
        "none": "none",
        "不下载": "none",
    }
    if choice not in aliases:
        raise RuntimeError(f"invalid_download_choice:{choice}")
    return aliases[choice]


def main() -> int:
    args = parse_args()
    if args.resume_run_dir:
        run_dir = Path(args.resume_run_dir).expanduser().resolve()
        if not run_dir.exists():
            raise RuntimeError(f"resume_run_dir_not_found:{run_dir}")
        collect_payload: dict[str, Any] = {"paths": {}}
    else:
        if not args.target:
            raise RuntimeError("missing_target_or_resume_run_dir")
        out_dir = Path(args.out_dir).expanduser().resolve()
        collect_cmd = [
            sys.executable,
            str(SCRIPT_DIR / "collect_creator_assets.py"),
            args.target,
            "--count",
            str(max(args.count, 1)),
            "--out-dir",
            str(out_dir),
        ]
        collect_result = run_command(collect_cmd)
        collect_payload = parse_collect_stdout(collect_result.stdout)
        raw_path = Path(collect_payload["paths"]["json"])
        run_dir = raw_path.parent

    candidate_path = ""
    download_result_path = ""
    download_mode = args.download_mode
    if download_mode == "ask":
        chosen_mode = ask_download_mode(run_dir, args)
        if chosen_mode is None:
            return 0
        download_mode = chosen_mode

    if download_mode == "samples":
        sample_cmd = [
            sys.executable,
            str(SCRIPT_DIR / "select_video_samples.py"),
            str(run_dir),
            "--top-k",
            str(max(args.top_k, 1)),
        ]
        if args.max_candidates > 0:
            sample_cmd.extend(["--max-candidates", str(args.max_candidates)])
        run_command(sample_cmd)
        candidate_path = str(existing_output_path(run_dir, "video_sample_candidates.csv"))
    elif download_mode == "all-collected":
        candidate_path = str(write_all_candidates(run_dir))

    if download_mode != "none":
        download_cmd = [
            sys.executable,
            str(SCRIPT_DIR / "download_sample_videos.py"),
            str(run_dir),
            "--item-timeout",
            str(max(args.item_timeout, 0)),
        ]
        if args.overwrite:
            download_cmd.append("--overwrite")
        run_command(download_cmd)
        download_result_path = str(existing_output_path(run_dir, "downloaded_videos.csv"))

    summary = {
        "status": "ok",
        "run_dir": str(run_dir),
        "download_mode": download_mode,
        "collect_paths": collect_payload.get("paths", {}),
        "candidate_csv": candidate_path,
        "download_result_csv": download_result_path,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
