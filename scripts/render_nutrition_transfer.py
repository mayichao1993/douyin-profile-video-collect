#!/usr/bin/env python3
"""Render 2C nutrition-topic transfer predictions from 2A and 2B outputs."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path
from typing import Any

from output_names import existing_output_path, mirror_legacy, output_path


COLUMNS = [
    "已验证优质内容结构",
    "来源样本",
    "起量机制",
    "对应的家长问题",
    "营养品可转接议题",
    "必须保留的表达方式",
    "转接风险",
    "评论验证点",
]

METRICS = ["点赞数", "评论数", "收藏数", "分享数"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render nutrition_transfer_prediction.csv/md from 2A coarse data and 2B video deep-dive outputs."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    parser.add_argument(
        "--max-items",
        type=int,
        default=5,
        help="Maximum verified content mechanisms to render.",
    )
    return parser.parse_args()


def read_csv_if_exists(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def safe_int(value: Any) -> int:
    try:
        return int(float(str(value or 0).strip()))
    except Exception:
        return 0


def text(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def split_paths(value: str) -> list[str]:
    paths: list[str] = []
    for part in value.replace("\n", "；").split("；"):
        stripped = part.strip()
        if stripped:
            paths.append(stripped)
    return paths


def path_exists_in_run_dir(run_dir: Path, path_text: str) -> bool:
    if not path_text:
        return False
    path = Path(path_text)
    if not path.exists():
        return False
    try:
        path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return False
    return True


def has_image_evidence(run_dir: Path, row: dict[str, str]) -> bool:
    manifest = text(row, "图片清单文件")
    if path_exists_in_run_dir(run_dir, manifest):
        return True
    return any(path_exists_in_run_dir(run_dir, path) for path in split_paths(text(row, "图片文件")))


def media_evidence_issue(run_dir: Path, row: dict[str, str]) -> str:
    media_type = text(row, "媒体类型")
    if media_type == "video":
        grid = text(row, "抽帧图")
        if path_exists_in_run_dir(run_dir, grid):
            return ""
        return "视频样本缺少具体抽帧图"
    if media_type == "image":
        if has_image_evidence(run_dir, row):
            return ""
        return "图文样本缺少具体图片文件"
    grid = text(row, "抽帧图")
    if path_exists_in_run_dir(run_dir, grid):
        return ""
    if has_image_evidence(run_dir, row):
        return ""
    return "媒体类型不明且缺少可查看的抽帧图/图片文件"


def ensure_deep_dive_ready(run_dir: Path) -> None:
    deep_path = existing_output_path(run_dir, "video_content_deep_dive.csv")
    deep_rows = read_csv_if_exists(deep_path)
    if not deep_rows:
        raise RuntimeError("missing_2b_deep_dive: 2C 必须在 2B 看完具体抽帧图/图片并生成细看明细后执行。")
    issues = [
        f"{text(row, '作品ID')}:{media_evidence_issue(run_dir, row)}"
        for row in deep_rows
        if media_evidence_issue(run_dir, row)
    ]
    if issues:
        raise RuntimeError(
            "missing_media_evidence: 2C 未生成；2B 样本缺少具体媒体证据。"
            + "；".join(issues)
        )


def index_by_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        aweme_id = text(row, "作品ID", "aweme_id", "aweme_id_str")
        if aweme_id:
            indexed[aweme_id] = row
    return indexed


def medians(rows: list[dict[str, str]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for metric in METRICS:
        values = [safe_int(row.get(metric)) for row in rows]
        result[metric] = statistics.median(values) if values else 0
    return result


def verified_metrics(row: dict[str, str], candidate: dict[str, str], metric_medians: dict[str, float]) -> list[str]:
    anchors = text(row, "四项互动锚点") or text(candidate, "抽样锚点")
    verified: list[str] = []
    for metric in METRICS:
        label = metric.replace("数", "")
        value = safe_int(candidate.get(metric))
        median = metric_medians.get(metric, 0)
        if f"{label}高位" in anchors or (median and value >= median * 1.5):
            verified.append(f"{label}{value}")
    return verified


def classify_structure(row: dict[str, str]) -> str:
    title_core = " ".join(
        [
            text(row, "作品标题"),
            text(row, "媒体核心内容", "视频核心内容"),
        ]
    )
    blob = " ".join(
        [
            title_core,
            text(row, "前三秒"),
            text(row, "不划走理由"),
            text(row, "中段停留机制"),
            text(row, "达人说服方式"),
        ]
    )
    if any(word in blob for word in ["第三年", "三年", "多年", "长期", "用了", "真实观察", "不是新鲜劲"]):
        return "长期使用/真实观察"
    if any(word in title_core for word in ["常见问题", "买前", "怎么选"]):
        return "买前常见问题拆解"
    if any(word in title_core for word in ["避坑", "拒绝", "放弃", "别再", "别买", "清单", "候选"]):
        return "反面避坑 + 候选清单"
    if any(word in blob for word in ["适合", "年龄", "能不能", "会不会"]):
        return "买前常见问题拆解"
    if any(word in blob for word in ["避坑", "拒绝", "放弃", "别再", "别买", "清单", "候选"]):
        return "反面避坑 + 候选清单"
    if any(word in blob for word in ["孩子真实", "实际使用", "真的会用", "上手", "场景"]):
        return "孩子真实使用场景"
    if any(word in blob for word in ["亲子", "Vlog", "日常", "户外"]):
        return "亲子日常场景铺垫"
    return "已验证内容机制"


def transfer_for_structure(structure: str) -> tuple[str, str, str, str, str]:
    if structure == "反面避坑 + 候选清单":
        return (
            "先否定一个家长容易踩的低价值选择，再给可判断、可筛选的替代清单。",
            "家长怕买错、怕买回没用、需要别人先帮她筛一轮。",
            "儿童营养品别乱买：孩子状态不好时，先看饮食、作息、运动和营养支持分别该怎么判断。",
            "保留妈妈经验、避坑口吻、选择标准和清单感。",
            "容易变成直接推荐某款营养品；如果用健康恐吓开头，会伤信任。",
        )
    if structure == "买前常见问题拆解":
        return (
            "把一个看起来复杂的选择拆成家长买前最想确认的几个问题。",
            "家长不是只想知道东西好不好，而是想知道适不适合、会不会踩坑、买了怎么用。",
            "儿童营养品买前问题：适合多大、怎么吃、看什么成分、安全吗、什么情况先不买。",
            "保留问答结构、入门建议、先判断需求再选择的表达。",
            "容易写成成分课或专家课；如果没有生活场景，会变硬。",
        )
    if structure == "长期使用/真实观察":
        return (
            "用长期使用或持续观察证明这不是一时新鲜，也不是摆拍推荐。",
            "家长想确认买完以后是否真的有用、能不能长期坚持、怎么观察变化。",
            "儿童状态观察逻辑：不讲立刻见效，讲家长怎么观察吃饭、运动、换季、睡前等日常状态。",
            "保留长期观察、使用边界、真实生活场景和不夸大效果的口吻。",
            "不能承诺功效；不能把营养品说成立刻见效。",
        )
    if structure == "孩子真实使用场景":
        return (
            "让孩子出现在真实日常里，用画面回答家长担心的使用问题。",
            "家长想确认这件事会不会落到孩子身上，而不是大人自嗨。",
            "把营养问题放进日常场景：吃饭、户外活动、换季、挑食、精神状态，而不是只拍产品。",
            "保留孩子真实场景、妈妈旁观和具体行为细节。",
            "如果只拍产品或只讲成分，会丢掉原本有效的场景信任。",
        )
    if structure == "亲子日常场景铺垫":
        return (
            "用亲子日常建立同类家庭氛围，但本身不一定解决问题。",
            "家长愿意看生活状态，但不一定进入营养品选择。",
            "只能弱转接到日常状态观察，先铺孩子处境，再进入具体问题。",
            "保留自然生活场景，不要突然插入产品。",
            "如果没有具体问题，容易变成硬塞营养品。",
        )
    return (
        "内容有互动验证，但机制需要人工再拆细。",
        "家长问题待从评论或逐字稿里继续验证。",
        "暂时只保留为儿童营养品待验证议题。",
        "保留原内容里已经被互动验证的表达方式。",
        "不要从儿童内容直接跳到营养品推荐。",
    )


def build_rows(run_dir: Path, max_items: int) -> list[dict[str, str]]:
    deep_rows = read_csv_if_exists(existing_output_path(run_dir, "video_content_deep_dive.csv"))
    candidate_rows = read_csv_if_exists(existing_output_path(run_dir, "video_sample_candidates.csv"))
    candidate_map = index_by_id(candidate_rows)
    metric_medians = medians(candidate_rows)
    rows: list[dict[str, str]] = []
    seen_structures: set[str] = set()

    for row in deep_rows:
        aweme_id = text(row, "作品ID")
        candidate = candidate_map.get(aweme_id, {})
        metric_hits = verified_metrics(row, candidate, metric_medians)
        if not metric_hits:
            continue
        if not text(row, "S层命中人群") or text(row, "S层命中人群") == "待补充":
            continue
        structure = classify_structure(row)
        if structure in seen_structures:
            continue
        mechanism, parent_problem, topic, keep_style, risk = transfer_for_structure(structure)
        evidence = "、".join(metric_hits)
        rows.append(
            {
                "已验证优质内容结构": structure,
                "来源样本": f"{aweme_id}｜{text(row, '作品标题')}",
                "起量机制": f"{mechanism} 本轮数据验证：{evidence}。",
                "对应的家长问题": parent_problem,
                "营养品可转接议题": topic,
                "必须保留的表达方式": keep_style,
                "转接风险": risk,
                "评论验证点": "评论里是否出现挑食、长高、免疫、安全、适用年龄、怎么吃、什么情况需要营养支持等真实问题。",
            }
        )
        seen_structures.add(structure)
        if len(rows) >= max_items:
            break

    if rows:
        return rows
    return [
        {
            "已验证优质内容结构": "本轮未看到可支撑转接的高互动内容机制",
            "来源样本": "",
            "起量机制": "2B 样本没有同时满足互动验证和内容机制清晰两项条件。",
            "对应的家长问题": "待验证",
            "营养品可转接议题": "暂时只能保留为待验证方向",
            "必须保留的表达方式": "不要强行转接。",
            "转接风险": "从儿童内容直接跳到营养品推荐，会变成硬广。",
            "评论验证点": "先补评论验证或补看更多高互动视频。",
        }
    ]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def build_md(run_dir: Path, rows: list[dict[str, str]]) -> str:
    lines = [
        "# 优质内容到营养品的转接预判（2C）",
        "",
        f"- 来源目录：`{run_dir}`",
        "- 依据：2A 标题/话题/公开字段粗筛 + 2B 真实媒体内容细看",
        "- 口径：只判断已验证内容结构能否转成儿童营养品议题，不判断具体产品承接、转化率或合作价值。",
        "",
        "## 1. 2C 结论",
        "",
    ]
    if rows and rows[0]["已验证优质内容结构"].startswith("本轮未看到"):
        lines.append("本轮没有看到可支撑营养品转接的高互动内容机制，暂时只能保留为待验证方向。")
    else:
        structures = "、".join(row["已验证优质内容结构"] for row in rows)
        lines.append(
            f"本轮能借的不是泛泛的儿童内容，而是这些已经被互动验证过的内容结构：{structures}。"
        )
        lines.append("")
        lines.append("营养品转接要沿用原来有效的家长问题结构，不能突然变成成分课、功效承诺或直接推产品。")
    lines.extend(
        [
            "",
            "## 2. 转接明细",
            "",
            "| 已验证结构 | 来源样本 | 可转接议题 | 必须保留 | 风险 |",
            "|---|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['已验证优质内容结构']} | `{row['来源样本']}` | {row['营养品可转接议题']} | "
            f"{row['必须保留的表达方式']} | {row['转接风险']} |"
        )
    lines.extend(
        [
            "",
            "## 3. 下一步评论验证",
            "",
        ]
    )
    for row in rows:
        lines.append(f"- {row['已验证优质内容结构']}：{row['评论验证点']}")
    lines.extend(
        [
            "",
            "一句话：先借账号已经跑出来的内容结构，再把问题换成儿童营养品选择题；不要从“有儿童内容”直接跳到“能推营养品”。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    ensure_deep_dive_ready(run_dir)
    rows = build_rows(run_dir, args.max_items)
    csv_path = output_path(run_dir, "nutrition_transfer_prediction.csv")
    md_path = output_path(run_dir, "nutrition_transfer_prediction.md")
    write_csv(csv_path, rows)
    mirror_legacy(csv_path, run_dir, "nutrition_transfer_prediction.csv")
    md_path.write_text(build_md(run_dir, rows), encoding="utf-8")
    mirror_legacy(md_path, run_dir, "nutrition_transfer_prediction.md")
    print(f"wrote {csv_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
