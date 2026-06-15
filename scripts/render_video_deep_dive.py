#!/usr/bin/env python3
"""Render 2B media deep-dive outputs from agent-neutral JSONL results."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from output_names import existing_output_path, mirror_legacy, output_path


COLUMNS = [
    "作品ID",
    "作品标题",
    "四项互动锚点",
    "媒体类型",
    "视频文件",
    "图片文件",
    "图片清单文件",
    "抽帧图",
    "分析模式",
    "是否发现挂车",
    "挂车证据",
    "商品内容信号",
    "S层命中人群",
    "前三秒",
    "不划走理由",
    "前三秒文案类型",
    "三秒停留技巧",
    "后文是否接住前三秒",
    "中段停留机制",
    "媒体核心内容",
    "口播/字幕依据",
    "画面重点",
    "继续看方式",
    "商品说服方式",
    "达人说服方式",
    "内容真实感",
    "点赞为什么高/低",
    "评论为什么高/低",
    "收藏为什么高/低",
    "分享为什么高/低",
    "品类连接来源",
    "下一步评论验证点",
]

METRIC_COLUMNS = ["点赞数", "评论数", "收藏数", "分享数", "总互动"]

CN_INDEX = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render video_content_deep_dive.csv/md from JSONL produced by WorkBuddy or another media agent."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    parser.add_argument(
        "--results",
        default="video_understanding_results.jsonl",
        help="JSONL/JSON filename inside run_dir, or an absolute path.",
    )
    return parser.parse_args()


def read_csv_if_exists(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"missing results file: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [normalize_record(item) for item in data if isinstance(item, dict)]
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        if isinstance(data, dict):
            records.append(normalize_record(data))
    return records


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    if isinstance(record.get("result"), dict):
        merged = dict(record)
        nested = record["result"]
        merged.update(nested)
        return merged
    return record


def text(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = str(value or "").strip()
        if value:
            return value
    return ""


def index_by_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        aweme_id = text(row, "作品ID", "aweme_id", "aweme_id_str")
        if aweme_id:
            indexed[aweme_id] = row
    return indexed


def id_from_path(path_text: str) -> str:
    stem = Path(path_text).stem
    if stem.endswith("_grid"):
        stem = stem[:-5]
    if stem.endswith("_images"):
        stem = stem[:-7]
    if stem.startswith("2b_"):
        return stem[3:]
    return stem


def index_grids(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        aweme_id = id_from_path(text(row, "抽帧图") or text(row, "视频文件"))
        if aweme_id:
            indexed[aweme_id] = row
    return indexed


def normalize_path(run_dir: Path, path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if path.is_absolute():
        return str(path)
    if path.exists():
        return str(path.resolve())
    candidate = run_dir / path
    if candidate.exists():
        return str(candidate.resolve())
    return str(path)


def existing_path_or_blank(run_dir: Path, path_text: str) -> str:
    if not path_text:
        return ""
    normalized = normalize_path(run_dir, path_text)
    normalized_path = Path(normalized)
    if not normalized_path.exists():
        return ""
    try:
        normalized_path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return ""
    return str(normalized_path)


def split_paths(value: str) -> list[str]:
    paths: list[str] = []
    for part in value.replace("\n", "；").split("；"):
        stripped = part.strip()
        if stripped:
            paths.append(stripped)
    return paths


def fallback(value: str) -> str:
    return value if value else "待补充"


def meaningful_text(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = text(row, name)
        if value and value not in {"不适用", "待补充", "无"}:
            return value
    return ""


def is_cart_value(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"是", "yes", "y", "true", "1", "发现", "已发现"}


def analysis_mode(row: dict[str, str]) -> str:
    explicit = text(row, "分析模式")
    if explicit in {"挂车", "非挂车"}:
        return explicit
    return "挂车" if is_cart_value(text(row, "是否发现挂车")) else "非挂车"


def mode_specific_section(row: dict[str, str]) -> tuple[str, str]:
    mode = analysis_mode(row)
    if mode == "挂车":
        return "商品说服方式", fallback(meaningful_text(row, "商品说服方式", "达人说服方式"))
    return "继续看方式", fallback(meaningful_text(row, "继续看方式", "达人说服方式"))


def build_rows(run_dir: Path, results: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates = index_by_id(read_csv_if_exists(existing_output_path(run_dir, "video_sample_candidates.csv")))
    downloads = index_by_id(read_csv_if_exists(existing_output_path(run_dir, "downloaded_videos.csv")))
    grids = index_grids(read_csv_if_exists(existing_output_path(run_dir, "video_frame_grids.csv")))
    rows: list[dict[str, str]] = []

    for result in results:
        aweme_id = text(result, "作品ID", "aweme_id", "id")
        if not aweme_id:
            continue
        candidate = candidates.get(aweme_id, {})
        download = downloads.get(aweme_id, {})
        grid = grids.get(aweme_id, {})
        video_path = text(result, "视频文件") or text(download, "视频文件") or str(run_dir / f"2b_{aweme_id}.mp4")
        image_files = text(result, "图片文件") or text(download, "图片文件")
        image_manifest = text(result, "图片清单文件") or text(download, "图片清单文件") or str(run_dir / f"2b_{aweme_id}_images.json")
        grid_path = text(result, "抽帧图") or text(grid, "抽帧图") or str(run_dir / f"2b_{aweme_id}_grid.jpg")
        row: dict[str, str] = {
            "作品ID": aweme_id,
            "作品标题": text(result, "作品标题", "title") or text(candidate, "作品标题"),
            "四项互动锚点": text(result, "四项互动锚点") or text(candidate, "抽样锚点"),
            "媒体类型": text(result, "媒体类型") or text(download, "媒体类型"),
            "视频文件": existing_path_or_blank(run_dir, video_path),
            "图片文件": image_files,
            "图片清单文件": existing_path_or_blank(run_dir, image_manifest),
            "抽帧图": existing_path_or_blank(run_dir, grid_path),
            "分析模式": text(result, "分析模式") or ("挂车" if is_cart_value(text(candidate, "是否发现挂车")) else "非挂车"),
            "是否发现挂车": text(result, "是否发现挂车") or text(candidate, "是否发现挂车"),
            "挂车证据": text(result, "挂车证据") or text(candidate, "挂车证据"),
            "商品内容信号": text(result, "商品内容信号") or text(candidate, "商品内容信号"),
        }
        for column in COLUMNS:
            if column in row:
                continue
            if column == "媒体核心内容":
                row[column] = fallback(text(result, "媒体核心内容", "视频核心内容"))
            else:
                row[column] = fallback(text(result, column))
        rows.append(row)
    return rows


def has_image_evidence(row: dict[str, str]) -> bool:
    if row.get("图片清单文件") and Path(row["图片清单文件"]).exists():
        return True
    return any(Path(path).exists() for path in split_paths(row.get("图片文件", "")))


def media_evidence_issue(row: dict[str, str]) -> str:
    media_type = row.get("媒体类型") or ""
    if media_type == "video":
        if row.get("抽帧图") and Path(row["抽帧图"]).exists():
            return ""
        return "视频样本缺少具体抽帧图，不能生成 2B 正式判断"
    if media_type == "image":
        if has_image_evidence(row):
            return ""
        return "图文样本缺少具体图片文件，不能生成 2B 正式判断"
    if row.get("抽帧图") and Path(row["抽帧图"]).exists():
        return ""
    if has_image_evidence(row):
        return ""
    return "媒体类型不明且缺少可查看的抽帧图/图片文件"


def find_missing_media_evidence(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for row in rows:
        issue = media_evidence_issue(row)
        if not issue:
            continue
        missing.append(
            {
                "作品ID": row.get("作品ID", ""),
                "作品标题": row.get("作品标题", ""),
                "抽样锚点": row.get("四项互动锚点", ""),
                "媒体类型": row.get("媒体类型", ""),
                "缺失原因": issue,
                "视频文件": row.get("视频文件", ""),
                "图片文件": row.get("图片文件", ""),
                "抽帧图": row.get("抽帧图", ""),
                "处理动作": "先补下载/抽帧，再重新回填媒体理解结果",
            }
        )
    return missing


def write_missing_media_evidence(run_dir: Path, rows: list[dict[str, str]]) -> None:
    columns = ["作品ID", "作品标题", "抽样锚点", "媒体类型", "缺失原因", "视频文件", "图片文件", "抽帧图", "处理动作"]
    csv_path = output_path(run_dir, "media_evidence_missing.csv")
    md_path = output_path(run_dir, "media_evidence_missing.md")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    mirror_legacy(csv_path, run_dir, "media_evidence_missing.csv")
    lines = [
        "# 2B 待补看媒体证据清单",
        "",
        f"- 来源目录：`{run_dir}`",
        f"- 待补样本数：{len(rows)}",
        "- 规则：视频样本必须有具体抽帧图，图文样本必须有具体图片文件；缺证据的样本不得进入 2B/2C 正式判断。",
        "",
        "| 作品ID | 标题 | 媒体类型 | 缺失原因 | 处理动作 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        title = row["作品标题"].replace("|", "｜")
        lines.append(
            f"| `{row['作品ID']}` | {title} | {row['媒体类型']} | {row['缺失原因']} | {row['处理动作']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mirror_legacy(md_path, run_dir, "media_evidence_missing.md")


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def metric_table(run_dir: Path, rows: list[dict[str, str]]) -> list[str]:
    candidate_map = index_by_id(read_csv_if_exists(existing_output_path(run_dir, "video_sample_candidates.csv")))
    lines = [
        "| 作品ID | 抽样锚点 | 点赞 | 评论 | 收藏 | 分享 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        candidate = candidate_map.get(row["作品ID"], {})
        metrics = {field: text(candidate, field) for field in METRIC_COLUMNS}
        lines.append(
            f"| `{row['作品ID']}` | {row['四项互动锚点']} | {metrics['点赞数']} | "
            f"{metrics['评论数']} | {metrics['收藏数']} | {metrics['分享数']} |"
        )
    return lines


def metric_map(run_dir: Path) -> dict[str, dict[str, str]]:
    return index_by_id(read_csv_if_exists(existing_output_path(run_dir, "video_sample_candidates.csv")))


def candidate_metric(candidate: dict[str, str], field: str) -> str:
    return text(candidate, field) or "0"


def sample_overview(run_dir: Path, rows: list[dict[str, str]]) -> list[str]:
    candidates = metric_map(run_dir)
    lines = [
        "## 样本概览",
        "",
        "| 作品ID | 标题 | 抽选锚点 | 点赞 | 评论 | 收藏 | 分享 | 总互动 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        candidate = candidates.get(row["作品ID"], {})
        title = row["作品标题"].replace("|", "｜")
        lines.append(
            f"| `{row['作品ID']}` | {title} | {row['四项互动锚点']} | "
            f"{candidate_metric(candidate, '点赞数')} | {candidate_metric(candidate, '评论数')} | "
            f"{candidate_metric(candidate, '收藏数')} | {candidate_metric(candidate, '分享数')} | "
            f"{candidate_metric(candidate, '总互动')} |"
        )
    return lines


def section_title(index: int, title: str) -> str:
    label = CN_INDEX[index] if index < len(CN_INDEX) else str(index + 1)
    return f"### {label}、{title}"


def media_entry_label(row: dict[str, str]) -> str:
    return "首图/前两张图" if row.get("媒体类型") == "image" else "前三秒"


def title_or_subtitle_analysis(row: dict[str, str]) -> list[str]:
    lines = [section_title(3, "标题 / 字幕 / 画面关系"), ""]
    evidence = row.get("口播/字幕依据", "")
    if evidence and evidence != "待补充":
        lines.extend(
            [
                f"可见文字/口播的作用：{evidence}",
                "",
            ]
        )
    lines.extend(
        [
            f"画面重点：{row['画面重点']}",
            "",
        ]
    )
    return lines


def interaction_sections(row: dict[str, str]) -> list[str]:
    return [
        section_title(6, "四项互动归因"),
        "",
        f"#### 点赞 — {row['点赞为什么高/低']}",
        "",
        f"#### 评论 — {row['评论为什么高/低']}",
        "",
        f"#### 收藏 — {row['收藏为什么高/低']}",
        "",
        f"#### 分享 — {row['分享为什么高/低']}",
        "",
    ]


def comparison_table(rows: list[dict[str, str]]) -> list[str]:
    if len(rows) < 2:
        return []
    lines = [
        "## 多样本对比总结",
        "",
        "| 维度 | " + " | ".join(row["作品标题"].replace("|", "｜") for row in rows) + " |",
        "|---|" + "|".join("---" for _ in rows) + "|",
        "| S层 | " + " | ".join(row["S层命中人群"] for row in rows) + " |",
        "| 分析模式 | " + " | ".join(analysis_mode(row) for row in rows) + " |",
        "| 留人入口 | " + " | ".join(row["前三秒"] for row in rows) + " |",
        "| 中段承接 | " + " | ".join(row["中段停留机制"] for row in rows) + " |",
        "| 继续看/商品说服 | " + " | ".join(mode_specific_section(row)[1] for row in rows) + " |",
        "| 真实感 | " + " | ".join(row["内容真实感"] for row in rows) + " |",
        "",
    ]
    return lines


def build_md(run_dir: Path, rows: list[dict[str, str]]) -> str:
    lines = [
        "# 02B 媒体内容细看",
        "",
        "- 来源：本次 2B 运行结果",
        f"- 样本数：{len(rows)}",
        "- 查看方式：由外部媒体理解 Agent 查看 mp4/抽帧图/图片/字幕后回填 JSON，本脚本只负责渲染成 2B 报告。",
        "- 阅读口径：基于真实媒体内容输出，正文按固定深拆结构呈现；媒体文件路径保留在明细表，不放进正文。",
        "",
    ]
    lines.extend(sample_overview(run_dir, rows))
    lines.extend(["", "---", ""])
    for index, row in enumerate(rows):
        label = CN_INDEX[index] if index < len(CN_INDEX) else str(index + 1)
        candidate = metric_map(run_dir).get(row["作品ID"], {})
        total = candidate_metric(candidate, "总互动")
        like = candidate_metric(candidate, "点赞数")
        comment = candidate_metric(candidate, "评论数")
        collect = candidate_metric(candidate, "收藏数")
        share = candidate_metric(candidate, "分享数")
        lines.extend(
            [
                f"## 作品{label}：{row['作品标题']} — {row['四项互动锚点']}",
                "",
                f"**作品ID**：`{row['作品ID']}`",
                "",
                f"**总互动**：{total}（点赞 {like} + 评论 {comment} + 收藏 {collect} + 分享 {share}）",
                "",
                "---",
                "",
                section_title(0, "S层命中人群"),
                "",
                f"**人群画像**：{row['S层命中人群']}",
                "",
                section_title(1, f"{media_entry_label(row)}：为什么不划走"),
                "",
                f"**入口画面/内容**：{row['前三秒']}",
                "",
                f"**为什么愿意先停一下**：{row['不划走理由']}",
                "",
                f"**入口类型**：{row['前三秒文案类型']}",
                "",
                f"**停留技巧**：{row['三秒停留技巧']}",
                "",
                f"**后文承接判断**：{row['后文是否接住前三秒']}",
                "",
                section_title(2, "中段承接分析"),
                "",
                f"**媒体核心内容**：{row['媒体核心内容']}",
                "",
                f"**中段是否接住入口期待**：{row['中段停留机制']}",
                "",
            ]
        )
        lines.extend(title_or_subtitle_analysis(row))
        section_name, section_value = mode_specific_section(row)
        lines.extend(
            [
                section_title(4, section_name),
                "",
                section_value,
                "",
                section_title(5, "真实感"),
                "",
                row["内容真实感"],
                "",
            ]
        )
        lines.extend(interaction_sections(row))
        lines.extend(
            [
                section_title(7, "品类连接和评论验证"),
                "",
                f"**品类连接来源**：{row['品类连接来源']}",
                "",
                f"**下一步评论验证点**：{row['下一步评论验证点']}",
                "",
                "---",
                "",
            ]
        )
    lines.extend(comparison_table(rows))
    lines.extend(
        [
            "## 文件",
            "",
            f"- 2B 明细表：同目录 `{output_path(run_dir, 'video_content_deep_dive.csv').name}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    results_path = Path(args.results)
    if not results_path.is_absolute():
        results_path = run_dir / results_path
    results = read_results(results_path)
    rows = build_rows(run_dir, results)
    missing = find_missing_media_evidence(rows)
    if missing:
        write_missing_media_evidence(run_dir, missing)
        raise RuntimeError(
            "missing_media_evidence: 2B 正式报告未生成；视频必须先看具体抽帧图，图文必须先看具体图片。"
        )
    csv_path = output_path(run_dir, "video_content_deep_dive.csv")
    md_path = output_path(run_dir, "video_content_deep_dive.md")
    write_csv(csv_path, rows)
    mirror_legacy(csv_path, run_dir, "video_content_deep_dive.csv")
    md_path.write_text(build_md(run_dir, rows), encoding="utf-8")
    mirror_legacy(md_path, run_dir, "video_content_deep_dive.md")
    print(f"wrote {csv_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
