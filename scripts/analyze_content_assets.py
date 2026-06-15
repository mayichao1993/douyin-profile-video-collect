#!/usr/bin/env python3
"""Build a 2A coarse content-asset analysis from creator asset outputs."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path
from typing import Any

from output_names import existing_output_path, legacy_path, mirror_legacy, output_path


POST_COLUMNS = [
    "作品ID",
    "作品标题",
    "标题话题",
    "点赞数",
    "评论数",
    "收藏数",
    "分享数",
    "总互动",
    "内容主题",
    "内容对象",
    "内容场景",
    "是否发现挂车",
    "挂车证据",
    "商品内容信号",
    "商品信号证据",
    "品类连接母类",
    "连接强度",
    "连接理由",
    "互动表现",
    "互动校验结论",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze creator content assets from a run directory.")
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_csv_if_exists(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return read_csv(path)


def remove_generated(run_dir: Path, legacy_name: str) -> None:
    for path in {output_path(run_dir, legacy_name), legacy_path(run_dir, legacy_name)}:
        if path.exists():
            path.unlink()


def safe_int(value: Any) -> int:
    try:
        return int(float(str(value or 0).strip()))
    except Exception:
        return 0


def extract_topics(desc: str, raw_item: dict[str, Any] | None) -> list[str]:
    topics = re.findall(r"#([^\s#，。,.!！?？]+)", desc)
    if raw_item:
        for extra in raw_item.get("text_extra") or []:
            name = extra.get("hashtag_name")
            if name:
                topics.append(str(name))
    seen: set[str] = set()
    result: list[str] = []
    for topic in topics:
        if topic and topic not in seen:
            seen.add(topic)
            result.append(topic)
    return result


def has_any(text: str, words: list[str]) -> bool:
    return any(word.lower() in text.lower() for word in words)


def parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def has_cart_signal(raw_item: dict[str, Any] | None) -> tuple[str, str]:
    if not raw_item:
        return "未知", "缺少原始接口数据，无法判断挂车字段。"

    direct_fields = [
        "promotion",
        "simple_shop_seeding",
        "related_product",
        "product_info",
        "shop_info",
    ]
    evidence: list[str] = []
    for field in direct_fields:
        value = parse_maybe_json(raw_item.get(field))
        if value not in (None, "", [], {}, False, 0):
            evidence.append(field)

    conditional_fields = [
        "anchors",
        "anchor_info",
        "aweme_anchor_info",
        "commerce_config_data",
        "component_info_v2",
    ]
    component_keywords = [
        "product",
        "shop",
        "cart",
        "commodity",
        "ecom",
        "commerce",
        "anchor",
        "商品",
        "购物",
        "视频同款",
        "小店",
        "橱窗",
        "购买",
        "抖音商城",
    ]
    for field in conditional_fields:
        value = parse_maybe_json(raw_item.get(field))
        value_text = json.dumps(value, ensure_ascii=False).lower()
        if value not in (None, "", [], {}, False, 0) and any(
            keyword.lower() in value_text for keyword in component_keywords
        ):
            evidence.append(field)

    if evidence:
        return "是", "公开接口返回挂车/商品相关字段：" + "/".join(evidence)
    return "未发现", "公开接口未返回 anchors、商品锚点或有效电商字段；不等于确定没有挂车。"


def commercial_judgment(desc: str, topics: list[str]) -> tuple[str, str]:
    text = desc + " " + " ".join(topics)
    evidence: list[str] = []

    product_words = [
        "相机",
        "照相机",
        "自行车",
        "3D打印机",
        "打印机",
        "氨基丁酸",
        "corcs",
        "crocs",
        "颜色尺码",
        "尺码",
        "返场",
    ]
    recommendation_words = ["推荐", "好物", "清单", "用了", "喜欢", "买", "补了", "返场", "同款", "礼物分享", "分享"]
    activity_words = ["六一", "儿童节", "礼物", "大推荐"]

    topic_hits = [topic for topic in topics if has_any(topic, product_words + activity_words)]
    product_hits = [word for word in product_words if word.lower() in text.lower()]
    recommendation_hits = [word for word in recommendation_words if word.lower() in text.lower()]

    if topic_hits:
        evidence.append("标题话题：" + "/".join(topic_hits))
    if product_hits:
        evidence.append("商品/品类词：" + "/".join(product_hits))
    if recommendation_hits:
        evidence.append("推荐/种草语气：" + "/".join(recommendation_hits))

    gift_decision = has_any(text, ["礼物分享", "礼物清单", "高智礼物", "儿童节礼物", "六一礼物"])

    if product_hits and (recommendation_hits or topic_hits):
        return "强商品信号", "；".join(evidence)
    if gift_decision:
        if not evidence:
            evidence.append("礼物推荐内容")
        else:
            evidence.append("礼物推荐内容")
        return "强商品信号", "；".join(evidence)
    if topic_hits and has_any(text, recommendation_words):
        return "强商品信号", "；".join(evidence)
    if has_any(text, ["产品", "测评", "开箱", "体验"]):
        evidence.append("产品展示/测评语气")
        return "弱商品信号", "；".join(evidence)
    return "无明显商品信号", ""


def content_theme(desc: str, topics: list[str]) -> tuple[str, str, str]:
    text = desc + " " + " ".join(topics)
    if has_any(text, ["氨基丁酸", "睡觉管理"]):
        return "儿童健康/状态管理", "孩子/家庭", "健康"
    if has_any(text, ["相机", "照相机", "3D打印机", "儿童节", "礼物", "玩具"]):
        return "儿童礼物/儿童用品推荐", "孩子/家庭", "消费"
    if has_any(text, ["自行车", "骑行"]):
        return "儿童户外/家庭活动", "孩子/家庭", "日常"
    if has_any(text, ["Family day", "周末", "日常", "户外"]):
        return "亲子日常/家庭陪伴", "家庭", "日常"
    if has_any(text, ["返场", "颜色", "尺码", "corcs", "crocs"]):
        return "成人/家庭消费返场", "泛人群", "消费"
    if has_any(text, ["小朋友", "孩子", "家长"]):
        return "育儿事件/亲子处境", "孩子/妈妈", "情绪"
    return "泛生活内容", "泛人群", "其他"


def category_connection(desc: str, topics: list[str], theme: str, obj: str, scene: str) -> tuple[str, str, str]:
    text = desc + " " + " ".join(topics) + " " + theme
    mothers: list[str] = []
    if has_any(text, ["家长", "亲子", "带娃", "喂养", "辅食", "宝宝"]):
        mothers.append("母婴")
    if has_any(text, ["小朋友", "孩子", "儿童", "儿童节", "儿童用品", "玩具", "相机", "自行车", "3D打印机"]):
        mothers.append("儿童")
    if has_any(text, ["氨基丁酸", "挑食", "辅食", "维生素", "矿物质", "长高", "免疫", "消化"]):
        mothers.append("营养")
    if has_any(text, ["氨基丁酸", "健康", "体质", "免疫", "肠胃", "过敏", "运动", "骑行", "睡觉管理"]):
        mothers.append("健康")
    if has_any(text, ["睡觉管理", "头等大事", "选择", "反思", "冤枉", "家长", "发育", "长不好", "吃不好"]):
        mothers.append("育儿焦虑")

    if not mothers:
        return "无", "无", "内容没有指向母婴、儿童、营养、健康或育儿焦虑。"

    if has_any(text, ["氨基丁酸", "挑食", "喂养", "辅食", "长高", "免疫", "消化", "睡觉管理"]):
        strength = "强"
        reason = "内容主体是孩子/家庭，且问题可自然接到儿童健康、营养或育儿焦虑决策。"
    elif "儿童" in mothers and scene in {"消费", "日常"}:
        strength = "中"
        reason = "内容主体和孩子/家庭有关，但主要是用品、礼物或亲子日常，不直接指向营养健康。"
    elif "母婴" in mothers or "儿童" in mothers:
        strength = "弱"
        reason = "内容提到孩子或家庭，但核心仍偏泛生活/消费表达。"
    else:
        strength = "无"
        reason = "没有足够的母婴营养品承接空间。"
    return "/".join(mothers), strength, reason


def interaction_band(total: int, median_total: float) -> str:
    if median_total <= 0:
        return "样本不足"
    if total >= median_total * 1.5:
        return "高于中位"
    if total <= median_total * 0.7:
        return "低于中位"
    return "接近中位"


def interaction_validation(row: dict[str, str], theme: str, band: str, median_total: float) -> str:
    likes = safe_int(row.get("点赞数"))
    comments = safe_int(row.get("评论数"))
    favorites = safe_int(row.get("收藏数"))
    shares = safe_int(row.get("分享数"))
    total = safe_int(row.get("总互动"))
    data_text = f"点赞{likes}、评论{comments}、收藏{favorites}、分享{shares}、总互动{total}"
    if band == "低于中位":
        return (
            f"内容上打{theme}，但{data_text}，低于样本中位{median_total:g}；"
            "初判这个方向在博主当前粉丝里响应不强，可能是对应人群不多，不能把它当账号稳定主轴。"
        )
    if band == "高于中位":
        return (
            f"内容上打{theme}，且{data_text}，高于样本中位{median_total:g}；"
            "说明这类内容在该账号粉丝里被验证过，可以作为后续重点样本。"
        )
    if band == "接近中位":
        return (
            f"内容上打{theme}，{data_text}，接近样本中位{median_total:g}；"
            "说明有基本反馈，但还不足以证明这是账号最强人群方向。"
        )
    return f"内容上打{theme}，但样本互动基准不足；先保留内容判断，不做受众强弱结论。"


def build_analysis(
    rows: list[dict[str, str]], source_dir: Path, median_total: float, source_label: str
) -> str:
    total = len(rows)
    commercial_count = sum(1 for row in rows if row["商品内容信号"] == "强商品信号")
    strong_count = sum(1 for row in rows if row["连接强度"] == "强")
    middle_count = sum(1 for row in rows if row["连接强度"] == "中")
    high_rows = [row for row in rows if row["互动表现"] == "高于中位"]
    clear_commercial_high = [row for row in high_rows if row["商品内容信号"] == "强商品信号"]
    commercial_totals = [
        safe_int(row.get("_total_interaction", 0))
        for row in rows
        if row["商品内容信号"] == "强商品信号"
    ]
    noncommercial_totals = [
        safe_int(row.get("_total_interaction", 0))
        for row in rows
        if row["商品内容信号"] != "强商品信号"
    ]
    commercial_median = float(statistics.median(commercial_totals)) if commercial_totals else 0.0
    noncommercial_median = float(statistics.median(noncommercial_totals)) if noncommercial_totals else 0.0

    theme_counts: dict[str, int] = {}
    for row in rows:
        theme_counts[row["内容主题"]] = theme_counts.get(row["内容主题"], 0) + 1
    sorted_themes = sorted(theme_counts.items(), key=lambda x: -x[1])
    theme_text = "；".join(f"{theme} {count}条" for theme, count in sorted_themes)
    main_theme = sorted_themes[0][0] if sorted_themes else "样本不足"

    if strong_count:
        first_judgment = (
            f"该账号内容资产和母婴营养品存在强连接样本，但不是全账号主轴；"
            f"{strong_count}条强连接主要来自儿童健康/状态管理。"
        )
    elif middle_count:
        first_judgment = (
            "该账号内容资产以儿童用品、儿童礼物和亲子日常为主，和“儿童”有中连接，"
            "但与营养、健康、育儿焦虑的直接连接不足。"
        )
    else:
        first_judgment = "该账号内容资产和母婴营养品连接偏弱，暂时缺少继续深挖的自然入口。"

    commercial_judgment = (
        f"商品内容信号明显：{commercial_count}/{total}条被判为强商品信号。"
        if total
        else "样本不足。"
    )
    if clear_commercial_high:
        commercial_judgment += (
            f" 高互动作品中也存在强商品信号，如《{clear_commercial_high[0]['作品标题']}》，"
            "说明粉丝会看她推荐儿童用品/礼物。"
        )

    if len(noncommercial_totals) >= 2:
        commercial_compare = (
            f"强商品信号内容中位互动为{commercial_median}，非强商品信号内容中位互动为{noncommercial_median}。"
        )
    elif noncommercial_totals:
        commercial_compare = (
            f"强商品信号内容中位互动为{commercial_median}；非强商品信号样本只有{len(noncommercial_totals)}条，"
            "不能稳定比较，但目前看商品推荐内容没有明显被粉丝排斥。"
        )
    else:
        commercial_compare = "样本几乎全是商品推荐内容，无法和非商品内容做稳定对照。"

    if high_rows:
        high_theme_counts: dict[str, int] = {}
        for row in high_rows:
            high_theme_counts[row["内容主题"]] = high_theme_counts.get(row["内容主题"], 0) + 1
        high_main_theme, high_main_count = max(high_theme_counts.items(), key=lambda x: x[1])
        if high_main_count >= 2:
            high_source_judgment = (
                f"高互动主要来自固定主题：{high_main_theme}，不是单纯偶发爆款。"
            )
        else:
            high_source_judgment = "高互动来源较分散，暂时更像单条内容机会，不足以判断稳定内容能力。"
    else:
        high_source_judgment = "没有高于中位的作品，无法判断高互动来源。"

    event_words = ["冤枉", "反思", "评论区", "争议", "吐槽", "情绪"]
    event_high = [row for row in high_rows if has_any(row["作品标题"], event_words)]
    if event_high:
        burst_judgment = "高互动里存在事件/情绪型内容，后续要区分事件热度和账号稳定内容能力。"
    elif clear_commercial_high:
        burst_judgment = "本轮高互动主要来自儿童礼物推荐，不是明显事件争议或纯情绪内容。"
    else:
        burst_judgment = "本轮高互动暂未显示明显事件/争议驱动。"

    if strong_count:
        category_judgment = (
            "有进入下一步的品类连接入口，但账号主轴不是营养健康。后续要看强连接作品的评论里，"
            "有没有真实的孩子适用、效果、成分、安全性问题。"
        )
    elif middle_count:
        category_judgment = (
            "和儿童/家庭场景有连接，但直接营养健康连接不足。后续如果要继续看，重点验证粉丝是否会在评论里问孩子适用和购买建议。"
        )
    else:
        category_judgment = "暂时缺少母婴营养品方向的自然入口，不建议继续放大判断。"

    next_step = (
        "建议进入评论信任盘，但评论优先级应放在儿童用品/儿童礼物和儿童健康相关作品上，"
        "重点看粉丝是在问怎么买、适不适合孩子，还是只是在泛泛互动。"
    )
    high_titles = [row["作品标题"] for row in high_rows[:3]]
    strong_titles = [row["作品标题"] for row in rows if row["连接强度"] == "强"]
    low_relevant_rows = [
        row
        for row in rows
        if row["连接强度"] in {"强", "中"} and row["互动表现"] == "低于中位"
    ]
    product_signal_count = commercial_count

    if product_signal_count >= max(1, total * 0.6):
        product_signal_plain = (
            f"近{total}条里有{product_signal_count}条是在推荐商品或儿童礼物，"
            "说明这个账号不是单纯记录生活，粉丝本来就会看到她推荐东西。"
        )
    else:
        product_signal_plain = (
            f"近{total}条里有{product_signal_count}条带商品推荐信号，"
            "商品内容不是账号主轴，需要谨慎看粉丝接受度。"
        )

    if strong_titles:
        category_plain = (
            "有继续看的入口，但不要把账号直接当营养健康号。更值得看的入口是：孩子状态管理、家长选择焦虑、"
            f"以及类似《{strong_titles[0]}》这种儿童健康处境。"
        )
    elif middle_count:
        category_plain = (
            "有弱连接，主要来自“给孩子选东西”这个场景；但如果直接讲营养、功效，会很突兀。"
        )
    else:
        category_plain = "暂时缺少母婴营养品方向的自然入口。"

    if low_relevant_rows:
        low_examples = "；".join(
            f"《{row['作品标题']}》打{row['内容主题']}但互动低"
            for row in low_relevant_rows[:2]
        )
        audience_validation = (
            f"需要注意：{low_examples}。这类内容方向本身相关，但没有被互动数据验证，"
            "初判该账号当前粉丝里对应人群不多，不能只因标题相关就放大判断。"
        )
    elif high_rows:
        audience_validation = (
            "当前高互动样本能说明粉丝会回应部分儿童/商品推荐内容；后续要继续用评论确认他们是在认真决策，"
            "还是只是被礼物清单、节日节点带动。"
        )
    else:
        audience_validation = "当前缺少高互动样本，内容方向只能保留为可能入口，不能判断粉丝人群已经被验证。"

    return "\n".join(
        [
            "# 账号内容资产粗筛（2A）",
            "",
            f"- 来源目录：`{source_dir}`",
            f"- 样本口径：基于{source_label}，共{total}条；本轮未打开真实媒体内容，只做标题/话题/公开字段粗筛。",
            f"- 中位总互动：{median_total}",
            "",
            "## 1. 初步内容资产判断",
            "",
            f"- 这个账号主要不是在讲营养健康，而是在给家长做儿童礼物/儿童用品推荐。{product_signal_plain}",
            f"- 品类连接判断：{category_plain}",
            f"- 互动校验：{audience_validation}",
            "- 下一步先看评论：粉丝是在问“怎么买/适不适合孩子”，还是只是在点赞凑热闹。",
            "",
            "## 2. 她主要在讲什么",
            "",
            f"- 近{total}条作品的主轴是：{main_theme}。",
            "- 她最近主要在推荐儿童用品和六一礼物，不是专门讲孩子吃喝健康的号。",
            f"- {theme_text}",
            "",
            "## 3. 哪类内容更容易起量",
            "",
            f"- {high_source_judgment}",
            f"- {burst_judgment}",
            *[f"- 高互动样本：{title}" for title in high_titles],
            "",
            "## 4. 粉丝会不会接受她推荐东西",
            "",
            f"- {product_signal_plain}",
            f"- 互动对照：{commercial_compare}",
            "- 初判：粉丝至少能接受儿童用品/礼物类推荐；但这一步还不能证明能卖得动，只能说明这类内容在账号里不突兀。",
            "",
            "## 5. 品类连接怎么看",
            "",
            f"- 强连接内容：{strong_count}条；中连接内容：{middle_count}条；弱/无连接：{total - strong_count - middle_count}条。",
            f"- {category_judgment}",
            f"- 互动校验：{audience_validation}",
            "- 下一步只验证连接是否真实：看评论里有没有孩子适用、效果、成分、安全性、在哪里买这类问题。",
            "",
            "## 6. 下一步看什么评论",
            "",
            "- 先看儿童健康/状态管理那条：评论里有没有妈妈问孩子适用、效果、成分、安全性。",
            "- 再看高互动儿童礼物/用品推荐：评论里有没有人问怎么买、哪里买、孩子适不适合，判断粉丝是否真的会向她要购买建议。",
            "- 如果评论只是夸、玩梗、泛互动，没有真实决策问题，就不要放大商品推荐能力。",
        ]
    )


def analyze_rows(
    rows: list[dict[str, str]], items_by_id: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, str]], float]:
    totals = [safe_int(row.get("总互动")) for row in rows]
    median_total = float(statistics.median(totals)) if totals else 0.0

    output_rows: list[dict[str, str]] = []
    for row in rows:
        desc = row.get("作品标题", "")
        raw_item = items_by_id.get(str(row.get("作品ID")))
        topics = extract_topics(desc, raw_item)
        theme, obj, scene = content_theme(desc, topics)
        cart_status, cart_evidence = has_cart_signal(raw_item)
        if row.get("是否发现挂车"):
            cart_status = row.get("是否发现挂车", cart_status)
            cart_evidence = row.get("挂车证据", cart_evidence)
        commercial, evidence = commercial_judgment(desc, topics)
        if cart_status == "是":
            commercial = "强商品信号"
            evidence = "；".join(part for part in [evidence, cart_evidence] if part)
        mothers, strength, reason = category_connection(desc, topics, theme, obj, scene)
        band = interaction_band(safe_int(row.get("总互动")), median_total)
        output_rows.append(
            {
                "作品ID": row.get("作品ID", ""),
                "作品标题": desc,
                "标题话题": "/".join(topics),
                "点赞数": str(safe_int(row.get("点赞数"))),
                "评论数": str(safe_int(row.get("评论数"))),
                "收藏数": str(safe_int(row.get("收藏数"))),
                "分享数": str(safe_int(row.get("分享数"))),
                "总互动": str(safe_int(row.get("总互动"))),
                "内容主题": theme,
                "内容对象": obj,
                "内容场景": scene,
                "是否发现挂车": cart_status,
                "挂车证据": cart_evidence,
                "商品内容信号": commercial,
                "商品信号证据": evidence,
                "品类连接母类": mothers,
                "连接强度": strength,
                "连接理由": reason,
                "互动表现": band,
                "互动校验结论": interaction_validation(row, theme, band, median_total),
                "_total_interaction": str(safe_int(row.get("总互动"))),
            }
        )
    return output_rows, median_total


def write_content_outputs(
    *,
    rows: list[dict[str, str]],
    items_by_id: dict[str, dict[str, Any]],
    run_dir: Path,
    csv_name: str,
    md_name: str,
    source_label: str,
) -> tuple[Path, Path, int]:
    output_rows, median_total = analyze_rows(rows, items_by_id)
    out_csv = output_path(run_dir, csv_name)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=POST_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output_rows)
    mirror_legacy(out_csv, run_dir, csv_name)

    out_md = output_path(run_dir, md_name)
    out_md.write_text(build_analysis(output_rows, run_dir, median_total, source_label), encoding="utf-8")
    mirror_legacy(out_md, run_dir, md_name)
    return out_csv, out_md, len(output_rows)


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    posts_path = existing_output_path(run_dir, "creator_posts.csv")
    cart_posts_path = existing_output_path(run_dir, "cart_posts.csv")
    raw_path = run_dir / "raw.json"
    rows = read_csv_if_exists(posts_path)
    cart_rows_for_analysis = read_csv_if_exists(cart_posts_path)
    raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
    items_by_id = {str(item.get("aweme_id")): item for item in raw.get("items", [])}

    result = {
        "stage": "2A_content_asset_coarse_screen",
        "route": raw.get("analysis_route", {}).get("mode") if isinstance(raw.get("analysis_route"), dict) else "",
    }

    if rows:
        out_csv, out_md, row_count = write_content_outputs(
            rows=rows,
            items_by_id=items_by_id,
            run_dir=run_dir,
            csv_name="content_asset_posts.csv",
            md_name="content_asset_analysis.md",
            source_label="第一项非置顶、非挂车主页普通作品",
        )
        result.update({"posts_csv": str(out_csv), "analysis": str(out_md), "rows": row_count})
    else:
        remove_generated(run_dir, "content_asset_posts.csv")
        remove_generated(run_dir, "content_asset_analysis.md")

    cart_out_csv = cart_out_md = None
    cart_row_count = 0
    if cart_rows_for_analysis:
        cart_out_csv, cart_out_md, cart_row_count = write_content_outputs(
            rows=cart_rows_for_analysis,
            items_by_id=items_by_id,
            run_dir=run_dir,
            csv_name="cart_content_asset_posts.csv",
            md_name="cart_content_asset_analysis.md",
            source_label="第一项单独拆出的挂车短视频",
        )
        result.update(
            {
                "cart_posts_csv": str(cart_out_csv),
                "cart_analysis": str(cart_out_md),
                "cart_rows": cart_row_count,
            }
        )
    else:
        remove_generated(run_dir, "cart_content_asset_posts.csv")
        remove_generated(run_dir, "cart_content_asset_analysis.md")

    if not rows and not cart_rows_for_analysis:
        raise FileNotFoundError("Need creator_posts.csv or cart_posts.csv with at least one row.")

    print(
        json.dumps(result, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
