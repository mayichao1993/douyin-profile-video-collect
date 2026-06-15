#!/usr/bin/env python3
"""Select candidate videos for the 2B deep-dive sample set."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

from output_names import existing_output_path, mirror_legacy, output_path
from typing import Any


METRICS = [
    ("点赞数", "点赞"),
    ("评论数", "评论"),
    ("收藏数", "收藏"),
    ("分享数", "分享"),
]

METRIC_ACTIONS = {
    "点赞": "看账号最容易被认可的内容长什么样。",
    "评论": "看哪些内容能打开提问、争议或经验交换。",
    "收藏": "看哪些内容有复看、清单或买前参考价值。",
    "分享": "看哪些内容适合转给家人、朋友或同类家长一起判断。",
}

LOW_METRIC_ACTIONS = {
    "点赞": "反向看为什么这类内容没有获得基础认可。",
    "评论": "反向看为什么这类内容没有打开提问或讨论。",
    "收藏": "反向看为什么这类内容没有形成复看或买前参考价值。",
    "分享": "反向看为什么这类内容不适合转给共同决策人。",
}

OUTPUT_COLUMNS = [
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
    "内容主题",
    "品类连接母类",
    "连接强度",
    "是否发现挂车",
    "挂车证据",
    "商品内容信号",
    "商品信号证据",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select 2B candidate videos from a douyin_creator_assets run directory."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    parser.add_argument(
        "--top-k",
        type=int,
        default=2,
        help="How many high/low samples to mark for each interaction metric.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=0,
        help="Optional maximum number of candidates to output. 0 means no cap.",
    )
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def safe_int(value: Any) -> int:
    try:
        return int(float(str(value or 0).strip()))
    except Exception:
        return 0


def row_id(row: dict[str, str]) -> str:
    return row.get("作品ID") or row.get("aweme_id") or row.get("aweme_id_str") or ""


def get_value(row: dict[str, str], field: str) -> int:
    aliases = {
        "点赞数": ["点赞数", "点赞", "digg_count"],
        "评论数": ["评论数", "评论", "comment_count"],
        "收藏数": ["收藏数", "收藏", "collect_count"],
        "分享数": ["分享数", "分享", "share_count"],
        "总互动": ["总互动", "total_interaction"],
    }
    for key in aliases.get(field, [field]):
        if key in row:
            return safe_int(row.get(key))
    return 0


def text_field(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def route_mode(run_dir: Path) -> str:
    raw_path = run_dir / "raw.json"
    if not raw_path.exists():
        return ""
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    route = raw.get("analysis_route")
    if isinstance(route, dict):
        return str(route.get("mode") or "")
    return ""


def find_input_path(run_dir: Path) -> Path:
    cart_preferred = existing_output_path(run_dir, "cart_content_asset_posts.csv")
    if route_mode(run_dir) == "cart_only" and cart_preferred.exists():
        return cart_preferred
    preferred = existing_output_path(run_dir, "content_asset_posts.csv")
    if preferred.exists():
        return preferred
    if cart_preferred.exists():
        return cart_preferred
    normal_fallback = existing_output_path(run_dir, "creator_posts.csv")
    if normal_fallback.exists():
        return normal_fallback
    cart_fallback = existing_output_path(run_dir, "cart_posts.csv")
    if cart_fallback.exists():
        return cart_fallback
    raise FileNotFoundError(
        "Need content_asset_posts.csv/cart_content_asset_posts.csv or creator_posts.csv/cart_posts.csv in run_dir."
    )


def is_category_related(row: dict[str, str]) -> bool:
    mothers = text_field(row, "品类连接母类")
    strength = text_field(row, "连接强度")
    if strength == "强":
        return True
    related_words = ["母婴", "儿童", "营养", "健康", "育儿焦虑"]
    return any(word in mothers for word in related_words)


def add_anchor(
    selected: dict[str, dict[str, Any]],
    row: dict[str, str],
    anchor: str,
    reason: str,
) -> None:
    rid = row_id(row)
    if not rid:
        return
    item = selected.setdefault(rid, {"row": row, "anchors": [], "reasons": []})
    if anchor not in item["anchors"]:
        item["anchors"].append(anchor)
    if reason not in item["reasons"]:
        item["reasons"].append(reason)


def select_candidates(rows: list[dict[str, str]], top_k: int) -> list[dict[str, str]]:
    selected: dict[str, dict[str, Any]] = {}
    usable_rows = [row for row in rows if row_id(row)]
    if not usable_rows:
        return []

    for metric_field, metric_label in METRICS:
        sorted_rows = sorted(usable_rows, key=lambda row: get_value(row, metric_field), reverse=True)
        for row in sorted_rows[:top_k]:
            value = get_value(row, metric_field)
            add_anchor(
                selected,
                row,
                f"{metric_label}高位",
                f"{metric_label}{value}，{METRIC_ACTIONS[metric_label]}",
            )
        for row in reversed(sorted_rows[-top_k:]):
            value = get_value(row, metric_field)
            add_anchor(
                selected,
                row,
                f"{metric_label}低位",
                f"{metric_label}{value}，{LOW_METRIC_ACTIONS[metric_label]}",
            )

    total_values = [get_value(row, "总互动") for row in usable_rows]
    total_median = statistics.median(total_values) if total_values else 0
    for row in usable_rows:
        total = get_value(row, "总互动")
        if is_category_related(row):
            if total < total_median:
                add_anchor(
                    selected,
                    row,
                    "品类连接低互动",
                    "内容和母婴/儿童/营养/健康/育儿焦虑相关，但总互动低于样本中位，用于判断相关内容是否被粉丝响应。",
                )
            elif text_field(row, "连接强度") == "强":
                add_anchor(
                    selected,
                    row,
                    "强品类连接",
                    "内容和母婴/儿童/营养/健康/育儿焦虑有强连接，用于判断品类入口是否真实。",
                )

    candidates: list[dict[str, str]] = []
    for item in selected.values():
        row = item["row"]
        anchors = " / ".join(item["anchors"])
        reasons = "；".join(item["reasons"])
        candidates.append(
            {
                "建议下载": "是",
                "作品ID": row_id(row),
                "作品标题": text_field(row, "作品标题", "标题", "desc"),
                "抽样锚点": anchors,
                "抽样理由": reasons,
                "点赞数": str(get_value(row, "点赞数")),
                "评论数": str(get_value(row, "评论数")),
                "收藏数": str(get_value(row, "收藏数")),
                "分享数": str(get_value(row, "分享数")),
                "总互动": str(get_value(row, "总互动")),
                "内容主题": text_field(row, "内容主题"),
                "品类连接母类": text_field(row, "品类连接母类"),
                "连接强度": text_field(row, "连接强度"),
                "是否发现挂车": text_field(row, "是否发现挂车"),
                "挂车证据": text_field(row, "挂车证据"),
                "商品内容信号": text_field(row, "商品内容信号"),
                "商品信号证据": text_field(row, "商品信号证据"),
            }
        )

    candidates.sort(
        key=lambda row: (
            "高位" not in row["抽样锚点"],
            "品类连接低互动" not in row["抽样锚点"],
            -safe_int(row["总互动"]),
        )
    )
    return candidates


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_md(run_dir: Path, source_path: Path, rows: list[dict[str, str]]) -> str:
    lines = [
        "# 2B 媒体细看抽样候选清单",
        "",
        f"- 来源目录：`{run_dir}`",
        f"- 输入文件：`{source_path.name}`",
        f"- 候选数量：{len(rows)}",
        "",
        "## 使用方式",
        "",
        "先下载这些候选媒体，再做 2B 真实内容细看。样本可能是视频，也可能是图片/图文。抽样不能只看总互动，必须覆盖点赞、评论、收藏、分享的高位/低位，以及品类连接但互动低的样本。",
        "",
        "## 候选媒体",
        "",
        "| 建议下载 | 作品ID | 标题 | 抽样锚点 | 抽样理由 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        title = row["作品标题"].replace("|", "｜")
        reason = row["抽样理由"].replace("|", "｜")
        lines.append(
            f"| {row['建议下载']} | `{row['作品ID']}` | {title} | {row['抽样锚点']} | {reason} |"
        )
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "1. 下载候选媒体。",
            "2. 视频作品抽帧；图片/图文作品直接查看图片组。",
            "3. 生成 `02B_媒体内容细看明细.csv` 和 `02B_媒体内容细看.md`。",
            "4. 基于 2A + 2B 再生成 2C `02C_营养品议题转接预判明细.csv/md`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    source_path = find_input_path(run_dir)
    rows = read_csv(source_path)
    candidates = select_candidates(rows, top_k=max(args.top_k, 1))
    if args.max_candidates > 0:
        candidates = candidates[: args.max_candidates]

    csv_path = output_path(run_dir, "video_sample_candidates.csv")
    md_path = output_path(run_dir, "video_sample_candidates.md")
    write_csv(csv_path, candidates)
    mirror_legacy(csv_path, run_dir, "video_sample_candidates.csv")
    md_path.write_text(build_md(run_dir, source_path, candidates), encoding="utf-8")
    mirror_legacy(md_path, run_dir, "video_sample_candidates.md")
    print(f"wrote {csv_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
