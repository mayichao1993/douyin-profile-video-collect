#!/usr/bin/env python3
"""Build an agent-neutral handoff package for 2B media understanding."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from output_names import existing_output_path, mirror_legacy, output_path


SCHEMA_FIELDS = [
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

METRIC_FIELDS = ["点赞数", "评论数", "收藏数", "分享数", "总互动"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a provider-neutral 2B media understanding handoff from downloaded media and frame grids."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    parser.add_argument("--candidates", default="video_sample_candidates.csv")
    parser.add_argument("--downloads", default="downloaded_videos.csv")
    parser.add_argument("--grids", default="video_frame_grids.csv")
    parser.add_argument(
        "--transcript-dir",
        default="",
        help="Optional directory containing transcript files. Defaults to run_dir.",
    )
    return parser.parse_args()


def read_csv_if_exists(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def text(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def id_from_path(path_text: str) -> str:
    stem = Path(path_text).stem
    if stem.endswith("_grid"):
        stem = stem[:-5]
    if stem.endswith("_images"):
        stem = stem[:-7]
    if stem.startswith("2b_"):
        return stem[3:]
    return stem


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


def index_downloads(run_dir: Path, rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        aweme_id = text(row, "作品ID", "aweme_id")
        if aweme_id:
            indexed[aweme_id] = row
    for video_path in sorted(run_dir.glob("2b_*.mp4")):
        aweme_id = id_from_path(str(video_path))
        indexed.setdefault(aweme_id, {})
        indexed[aweme_id].setdefault("视频文件", str(video_path))
        indexed[aweme_id].setdefault("媒体类型", "video")
        indexed[aweme_id].setdefault("下载状态", "已存在")
    for manifest_path in sorted(run_dir.glob("2b_*_images.json")):
        aweme_id = id_from_path(str(manifest_path))
        indexed.setdefault(aweme_id, {})
        indexed[aweme_id].setdefault("图片清单文件", str(manifest_path))
        indexed[aweme_id].setdefault("媒体类型", "image")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            image_files = manifest.get("image_files") or []
            if isinstance(image_files, list):
                indexed[aweme_id].setdefault("图片文件", "；".join(str(path) for path in image_files))
        except Exception:
            pass
    return indexed


def index_grids(run_dir: Path, rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        grid = text(row, "抽帧图")
        video = text(row, "视频文件")
        aweme_id = id_from_path(grid or video)
        if aweme_id:
            indexed[aweme_id] = row
    for grid_path in sorted(run_dir.glob("2b_*_grid.jpg")):
        aweme_id = id_from_path(str(grid_path))
        indexed.setdefault(aweme_id, {})
        indexed[aweme_id].setdefault("抽帧图", str(grid_path))
        indexed[aweme_id].setdefault("抽帧状态", "已存在")
    return indexed


def find_transcript(transcript_dir: Path, aweme_id: str) -> str:
    patterns = [
        f"2b_{aweme_id}.transcript.txt",
        f"2b_{aweme_id}.transcript.md",
        f"2b_{aweme_id}.transcript.json",
        f"{aweme_id}.transcript.txt",
        f"{aweme_id}.transcript.md",
        f"transcript_{aweme_id}.txt",
        f"transcript_{aweme_id}.md",
    ]
    for pattern in patterns:
        path = transcript_dir / pattern
        if path.exists():
            return str(path)
    return ""


def split_paths(value: str) -> list[str]:
    paths: list[str] = []
    for part in value.replace("\n", "；").split("；"):
        stripped = part.strip()
        if stripped:
            paths.append(stripped)
    return paths


def image_paths_from_manifest(path_text: str) -> list[str]:
    if not path_text:
        return []
    path = Path(path_text)
    if not path.exists():
        return []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    image_files = manifest.get("image_files") or []
    if not isinstance(image_files, list):
        return []
    return [str(item) for item in image_files if item]


def expected_schema() -> dict[str, str]:
    schema = {
        field: "必填。看真实媒体素材后用人话填写，不要写空泛词。"
        for field in SCHEMA_FIELDS
    }
    schema.update(
        {
            "S层命中人群": "写成“人群画像 + 她卡在哪一步 + 这条内容解决哪一步”，不要写宝妈/家长这类身份词。",
            "分析模式": "照交接包原值填写：挂车 或 非挂车。不要自行改判。",
            "是否发现挂车": "照交接包原值填写。只按公开字段，不自行猜测。",
            "挂车证据": "照交接包原值填写；没有就写未发现。",
            "商品内容信号": "照交接包原值填写。",
            "前三秒": "视频写前三秒画面证据；图文写首图/前两张图证据。必须能支撑为什么停留。",
            "不划走理由": "写用户心理，不要复述画面。说明她怕什么、想确认什么、为什么愿意先看下去。",
            "后文是否接住前三秒": "判断入口制造的期待有没有被后文兑现；没兑现要写断在哪里。",
            "中段停留机制": "写中段回答了什么问题、承接哪里强/弱；不要写多画面/信息密度高这种空词。",
            "口播/字幕依据": "不要只摘字幕。写标题/字幕/口播和画面如何配合，是否形成承诺和证据闭环。",
            "画面重点": "写画面证明了什么、没证明什么；不要列镜头清单。",
            "继续看方式": "分析模式=非挂车时必填：只写怎么让用户继续看下去，比如入口期待、中段回答、情绪推进、信息递进或画面递进；分析模式=挂车时写不适用。",
            "商品说服方式": "分析模式=挂车时必填：写商品如何出现、解决哪个购买/选择问题、哪里自然或生硬、是否影响评论/收藏/分享；分析模式=非挂车时写不适用。",
            "达人说服方式": "兼容旧字段，可留空；如果必须填写，按分析模式填对应内容：非挂车填继续看方式，挂车填商品说服方式。",
            "内容真实感": "写真实感评级和原因，并说明真实感有没有帮助用户继续看。",
            "点赞为什么高/低": "结合点赞数据解释认可、共鸣、真实感或价值认同为什么成立/不成立。",
            "评论为什么高/低": "结合评论数据解释有没有提问钩子、争议、经验交换或求链接入口。",
            "收藏为什么高/低": "结合收藏数据解释有没有复看价值、清单价值、步骤价值或购买前参考价值。",
            "分享为什么高/低": "结合分享数据解释有没有共同决策、转给家人/同类家长的理由。",
        }
    )
    return schema


def is_cart_value(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"是", "yes", "y", "true", "1", "发现", "已发现"}


def analysis_mode_from_row(row: dict[str, str]) -> str:
    return "挂车" if is_cart_value(text(row, "是否发现挂车")) else "非挂车"


def build_prompt(record: dict[str, Any]) -> str:
    media = record["media"]
    media_lines = [
        f"分析模式：{record['analysis_mode']}",
        f"是否发现挂车：{record['cart']['status'] or '未发现'}",
        f"挂车证据：{record['cart']['evidence'] or '无'}",
        f"商品内容信号：{record['content_coarse'].get('商品内容信号') or '无'}",
        f"媒体类型：{media['media_type'] or '未知'}",
        f"视频文件：{media['video_path'] or '无'}",
        f"图片文件：{media['image_paths'] or '无'}",
        f"抽帧图：{media['frame_grid_path'] or '无'}",
        f"字幕/转写：{media['transcript_path'] or '无'}",
        f"媒体证据状态：{media.get('evidence_note') or '无'}",
    ]
    return (
        "请查看这个抖音样本的媒体文件：如果 media_type=video，必须看具体抽帧图；如果 media_type=image，就看图片文件/图片清单。按 output_schema 输出一个 JSON 对象。\n"
        "结果会被渲染成 2B 深拆报告：S层命中人群 -> 前三秒/首图 -> 中段承接 -> 标题/字幕/画面关系 -> 第五段按分析模式二选一 -> 真实感 -> 四项互动归因。\n"
        "不要只复述画面。每条判断都按“媒体证据 -> 打中的家长问题 -> 互动数据是否验证 -> 对点赞/评论/收藏/分享的影响”来写。\n"
        "S层命中人群不要写成宝妈/家长这种身份词，要写清她现在卡在哪一步。\n"
        "视频作品要判断前三秒为什么用户愿意先停一下、属于哪类文案入口、用了什么停留技巧、后文有没有接住。\n"
        "图片/图文作品没有前三秒时，不要硬编前三秒；改写首图/前两张图如何留人、标题和图片顺序如何制造继续看的理由。\n"
        "中段停留机制不能写成多画面/真实感/信息密度高，必须写中段或后续图片回答了家长什么问题。\n"
        "如果分析模式=非挂车：只填“继续看方式”，看这条内容怎么让用户继续看下去，不要写商品/购买说服。\n"
        "如果分析模式=挂车：只填“商品说服方式”，看商品如何出现、解决哪个购买/选择问题、哪里自然或生硬、是否影响评论/收藏/分享。\n"
        "不要把两套混在一起：非挂车不写商品说服；挂车不写非商品继续看方式。\n"
        "如果内容和母婴/儿童/营养/健康/育儿焦虑相关但四项互动低，要写：方向相关，但该账号粉丝里对应人群响应不强或人群可能不多。\n"
        "只输出 JSON，不要输出 Markdown。\n\n"
        f"作品ID：{record['aweme_id']}\n"
        f"标题：{record['title']}\n"
        f"抽样锚点：{record['sampling']['anchors']}\n"
        f"互动数据：{record['metrics']}\n"
        + "\n".join(media_lines)
    )


def evidence_status(record: dict[str, Any]) -> tuple[bool, str]:
    media = record["media"]
    media_type = media.get("media_type") or ""
    if media_type == "video":
        if media.get("frame_grid_path"):
            return True, "视频样本已提供具体抽帧图"
        return False, "视频样本缺少具体抽帧图，必须先运行 extract_video_frames.py 生成 2b_<作品ID>_grid.jpg"
    if media_type == "image":
        if media.get("image_paths"):
            return True, "图文样本已提供具体图片文件"
        return False, "图文样本缺少图片文件，必须先下载图片组"
    if media.get("frame_grid_path") or media.get("image_paths"):
        return True, "已提供可查看媒体证据"
    return False, "媒体类型不明且缺少可查看的抽帧图/图片文件"


def build_records(
    run_dir: Path,
    transcript_dir: Path,
    *,
    candidates_name: str,
    downloads_name: str,
    grids_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = read_csv_if_exists(existing_output_path(run_dir, candidates_name))
    downloads = index_downloads(run_dir, read_csv_if_exists(existing_output_path(run_dir, downloads_name)))
    grids = index_grids(run_dir, read_csv_if_exists(existing_output_path(run_dir, grids_name)))
    schema = expected_schema()
    records: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for row in candidates:
        if text(row, "建议下载") not in {"", "是", "yes", "Y", "y", "1", "true", "True"}:
            continue
        aweme_id = text(row, "作品ID", "aweme_id")
        if not aweme_id:
            continue
        download_row = downloads.get(aweme_id, {})
        grid_row = grids.get(aweme_id, {})
        media_type = text(download_row, "媒体类型") or ("image" if text(download_row, "图片文件", "图片清单文件") else "video")
        local_video_path = str(run_dir / f"2b_{aweme_id}.mp4")
        video_path = normalize_path(run_dir, local_video_path if Path(local_video_path).exists() else text(download_row, "视频文件"))
        local_manifest_path = str(run_dir / f"2b_{aweme_id}_images.json")
        image_manifest_path = normalize_path(
            run_dir,
            local_manifest_path if Path(local_manifest_path).exists() else text(download_row, "图片清单文件"),
        )
        image_paths = [
            normalize_path(run_dir, path)
            for path in split_paths(text(download_row, "图片文件"))
        ]
        if not image_paths:
            image_paths = [normalize_path(run_dir, path) for path in image_paths_from_manifest(image_manifest_path)]
        grid_path = normalize_path(run_dir, text(grid_row, "抽帧图") or str(run_dir / f"2b_{aweme_id}_grid.jpg"))
        transcript_path = find_transcript(transcript_dir, aweme_id)
        record: dict[str, Any] = {
            "task_type": "douyin_creator_assets_2B_media_understanding",
            "aweme_id": aweme_id,
            "title": text(row, "作品标题", "标题"),
            "sampling": {
                "anchors": text(row, "抽样锚点"),
                "reason": text(row, "抽样理由"),
            },
            "metrics": {field: text(row, field) for field in METRIC_FIELDS},
            "content_coarse": {
                "内容主题": text(row, "内容主题"),
                "品类连接母类": text(row, "品类连接母类"),
                "连接强度": text(row, "连接强度"),
                "商品内容信号": text(row, "商品内容信号"),
                "商品信号证据": text(row, "商品信号证据"),
            },
            "cart": {
                "status": text(row, "是否发现挂车"),
                "evidence": text(row, "挂车证据"),
            },
            "media": {
                "media_type": media_type,
                "video_path": video_path if path_exists_in_run_dir(run_dir, video_path) else "",
                "image_paths": [path for path in image_paths if path_exists_in_run_dir(run_dir, path)],
                "image_manifest_path": image_manifest_path if path_exists_in_run_dir(run_dir, image_manifest_path) else "",
                "frame_grid_path": grid_path if path_exists_in_run_dir(run_dir, grid_path) else "",
                "transcript_path": transcript_path,
                "video_url_file": normalize_path(run_dir, str(run_dir / f"2b_{aweme_id}.url.txt")),
            },
            "output_schema": schema,
            "prompt": "",
        }
        record["analysis_mode"] = analysis_mode_from_row(row)
        ok, evidence_note = evidence_status(record)
        record["media"]["evidence_note"] = evidence_note
        record["prompt"] = build_prompt(record)
        if ok:
            records.append(record)
        else:
            missing.append(
                {
                    "作品ID": aweme_id,
                    "作品标题": record["title"],
                    "抽样锚点": record["sampling"]["anchors"],
                    "媒体类型": media_type,
                    "缺失原因": evidence_note,
                    "视频文件": record["media"]["video_path"],
                    "图片文件": "；".join(record["media"]["image_paths"]),
                    "抽帧图": record["media"]["frame_grid_path"],
                    "处理动作": "先补下载/抽帧，再生成正式 2B 媒体理解交接包",
                }
            )
    return records, missing


def write_missing_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = ["作品ID", "作品标题", "抽样锚点", "媒体类型", "缺失原因", "视频文件", "图片文件", "抽帧图", "处理动作"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build_missing_md(run_dir: Path, rows: list[dict[str, Any]]) -> str:
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
        title = str(row["作品标题"]).replace("|", "｜")
        lines.append(
            f"| `{row['作品ID']}` | {title} | {row['媒体类型']} | {row['缺失原因']} | {row['处理动作']} |"
        )
    return "\n".join(lines) + "\n"


def build_md(run_dir: Path, records: list[dict[str, Any]]) -> str:
    lines = [
        "# 2B 媒体理解 Agent 交接包",
        "",
        f"- 来源目录：`{run_dir}`",
        f"- 样本数：{len(records)}",
        "- 用途：交给 WorkBuddy 或其他能查看视频/图片/字幕的 Agent，按统一 JSON 字段回填 2B 细看结果。",
        "- 硬门槛：视频样本必须查看具体抽帧图；没有抽帧图的样本不进入本交接包，只进入待补看清单。",
        "",
        "## 回填要求",
        "",
        "每个媒体样本回填一个 JSON 对象，字段必须和 `output_schema` 一致。不要输出 Markdown，不要合并多条样本。",
        "",
        "关键判断链条：媒体证据 -> 打中的家长问题 -> 互动数据是否验证 -> 点赞/评论/收藏/分享哪个动作被激发或没被激发。",
        "",
        "两套模式：`分析模式=非挂车` 只写继续看方式；`分析模式=挂车` 只写商品说服方式。不要混写。",
        "",
        "## 样本",
        "",
        "| 作品ID | 标题 | 分析模式 | 挂车状态 | 抽样锚点 | 媒体类型 | 视频 | 图片 | 抽帧图 | 字幕/转写 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        media = record["media"]
        title = record["title"].replace("|", "｜")
        image_text = "；".join(media.get("image_paths") or [])
        lines.append(
            f"| `{record['aweme_id']}` | {title} | {record['analysis_mode']} | "
            f"{record['cart'].get('status') or '未发现'} | {record['sampling']['anchors']} | "
            f"{media.get('media_type', '')} | `{media['video_path']}` | `{image_text}` | "
            f"`{media['frame_grid_path']}` | `{media['transcript_path']}` |"
        )
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "1. 把 `02B_媒体理解交接包.jsonl` 交给媒体理解 Agent。",
            "2. 让 Agent 逐条输出 JSONL，建议文件名：`video_understanding_results.jsonl`。",
            "3. 运行 `render_video_deep_dive.py` 渲染 `02B_媒体内容细看明细.csv` 和 `02B_媒体内容细看.md`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    transcript_dir = Path(args.transcript_dir) if args.transcript_dir else run_dir
    records, missing = build_records(
        run_dir,
        transcript_dir,
        candidates_name=args.candidates,
        downloads_name=args.downloads,
        grids_name=args.grids,
    )
    if missing:
        missing_csv_path = output_path(run_dir, "media_evidence_missing.csv")
        missing_md_path = output_path(run_dir, "media_evidence_missing.md")
        write_missing_csv(missing_csv_path, missing)
        mirror_legacy(missing_csv_path, run_dir, "media_evidence_missing.csv")
        missing_md_path.write_text(build_missing_md(run_dir, missing), encoding="utf-8")
        mirror_legacy(missing_md_path, run_dir, "media_evidence_missing.md")
        print(f"wrote {missing_csv_path}")
        print(f"wrote {missing_md_path}")
    jsonl_path = output_path(run_dir, "video_understanding_handoff.jsonl")
    md_path = output_path(run_dir, "video_understanding_handoff.md")
    write_jsonl(jsonl_path, records)
    mirror_legacy(jsonl_path, run_dir, "video_understanding_handoff.jsonl")
    md_path.write_text(build_md(run_dir, records), encoding="utf-8")
    mirror_legacy(md_path, run_dir, "video_understanding_handoff.md")
    print(f"wrote {jsonl_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
