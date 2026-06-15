#!/usr/bin/env python3
"""Collect public Douyin creator/video interaction data for a creator asset table."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from output_names import mirror_legacy, output_path


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = SKILL_ROOT / "outputs" / "douyin_profile_video_collect"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36"
)
HOMEPAGE_CART_SCAN_COUNT = 20


class CollectError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Douyin public creator/video interaction data."
    )
    parser.add_argument("target", help="Douyin account share text, profile URL, video URL, or aweme_id.")
    parser.add_argument("--count", type=int, default=10, help="Target non-pinned works to collect for profile URLs.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output root directory.")
    return parser.parse_args()


def request_text(url: str, *, user_agent: str = USER_AGENT, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def resolve_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    with opener.open(req, timeout=30) as resp:
        return resp.geturl()


def extract_first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s]+", text)
    if not match:
        return None
    return match.group(0).rstrip("，。,:：;；)")


def normalize_target(target: str) -> str:
    target = target.strip()
    if re.fullmatch(r"\d{15,25}", target):
        return f"https://www.douyin.com/video/{target}"
    url = extract_first_url(target)
    if not url:
        raise CollectError("target_must_contain_douyin_url_or_aweme_id")
    if "douyin.com" not in url and "iesdouyin.com" not in url:
        raise CollectError("target_url_is_not_douyin")
    return url


def extract_aweme_id(url: str) -> str | None:
    patterns = [
        r"/video/(\d+)",
        r"/note/(\d+)",
        r"/share/video/(\d+)",
        r"/share/note/(\d+)",
        r"[?&]modal_id=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.fullmatch(r"\d{15,25}", url):
        return url
    return None


def extract_sec_uid(url: str) -> str | None:
    match = re.search(r"/user/([^/?#]+)", url)
    if match:
        return urllib.parse.unquote(match.group(1))
    match = re.search(r"[?&]sec_uid=([^&#]+)", url)
    if match:
        return urllib.parse.unquote(match.group(1))
    return None


def extract_router_data(page_html: str) -> dict[str, Any]:
    marker = "window._ROUTER_DATA = "
    start = page_html.find(marker)
    if start < 0:
        raise CollectError("router_data_not_found")
    start += len(marker)
    end = page_html.find("</script>", start)
    if end < 0:
        raise CollectError("router_data_script_not_closed")
    raw = page_html[start:end].strip()
    return json.loads(html.unescape(raw))


def item_from_mobile_share(aweme_id: str) -> dict[str, Any]:
    errors: list[str] = []
    for share_type in ["video", "note"]:
        url = f"https://www.iesdouyin.com/share/{share_type}/{aweme_id}/?from_ssr=1"
        try:
            page_html = request_text(url, user_agent=MOBILE_USER_AGENT)
            router = extract_router_data(page_html)
            loader = router.get("loaderData") or {}
            page_data = (
                loader.get(f"{share_type}_(id)/page")
                or loader.get("video_(id)/page")
                or loader.get("note_(id)/page")
                or {}
            )
            video_info = (
                page_data.get("videoInfoRes")
                or page_data.get("noteInfoRes")
                or page_data.get("awemeInfoRes")
                or {}
            )
            items = video_info.get("item_list") or video_info.get("aweme_list") or []
            if items:
                return items[0]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{share_type}:{exc}")
    raise CollectError("share_item_not_found:" + "；".join(errors))


def collect_profile_items_with_playwright(sec_uid: str, count: int) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise CollectError(f"playwright_not_available:{exc}") from exc

    collected: list[dict[str, Any]] = []
    cursor = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(f"https://www.douyin.com/user/{sec_uid}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            for _ in range(20):
                payload = page.evaluate(
                    """
async ({secUid, cursor, count}) => {
  const apiUrl = `/aweme/v1/web/aweme/post/?device_platform=webapp&aid=6383&channel=channel_pc_web&sec_user_id=${encodeURIComponent(secUid)}&max_cursor=${encodeURIComponent(cursor)}&locate_query=false&show_live_replay_strategy=1&need_time_list=1&time_list_query=0&whale_cut_token=&cut_version=1&count=${encodeURIComponent(count)}&publish_video_strategy_type=2&from_user_page=1`;
  const response = await fetch(apiUrl, { credentials: "include" });
  const body = await response.text();
  return { ok: response.ok, status: response.status, body };
}
""",
                    {
                        "secUid": sec_uid,
                        "cursor": cursor,
                        "count": min(20, max(HOMEPAGE_CART_SCAN_COUNT, count, 1)),
                    },
                )
                if not isinstance(payload, dict) or not payload.get("ok"):
                    raise CollectError(f"profile_api_http_error:{payload!r}")
                data = json.loads(payload.get("body") or "{}")
                items = data.get("aweme_list") or []
                collected.extend(items)

                provisional_rows = [
                    row_from_item(item, source="web_profile_post_api", profile_position=index + 1)
                    for index, item in enumerate(collected)
                ]
                annotate_pinned_rows(provisional_rows)
                first_twenty = [
                    row
                    for row in provisional_rows
                    if safe_int(row.get("profile_position")) <= HOMEPAGE_CART_SCAN_COUNT
                ]
                first_twenty_non_pinned = [
                    row for row in first_twenty if row.get("is_pinned") not in {"yes", "likely"}
                ]
                first_twenty_all_cart = bool(first_twenty_non_pinned) and all(
                    row.get("cart_status") == "yes" for row in first_twenty_non_pinned
                )
                if len(provisional_rows) >= HOMEPAGE_CART_SCAN_COUNT and (
                    len(baseline_rows(provisional_rows)) >= count or first_twenty_all_cart
                ):
                    break
                if not data.get("has_more") or not items:
                    break
                next_cursor = data.get("max_cursor") or data.get("min_cursor")
                if next_cursor is None or next_cursor == cursor:
                    break
                cursor = next_cursor
                page.wait_for_timeout(800)
        finally:
            browser.close()
    return collected


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def text_contains_cart_signal(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False).lower()
    keywords = [
        "product",
        "shop",
        "cart",
        "commodity",
        "ecom",
        "commerce",
        "商品",
        "购物",
        "视频同款",
        "小店",
        "橱窗",
        "购买",
        "去买",
        "同款",
        "团购",
        "抖音商城",
    ]
    return any(keyword in text for keyword in keywords)


def cart_signal_from_item(item: dict[str, Any]) -> tuple[str, str]:
    direct_fields = [
        "simple_shop_seeding",
        "related_product",
        "product_info",
        "shop_info",
        "promotion",
    ]
    evidence: list[str] = []
    for field in direct_fields:
        value = parse_maybe_json(item.get(field))
        if value not in (None, "", [], {}, False, 0):
            evidence.append(field)

    conditional_fields = [
        "anchors",
        "anchor_info",
        "aweme_anchor_info",
        "commerce_config_data",
        "component_info_v2",
    ]
    for field in conditional_fields:
        value = parse_maybe_json(item.get(field))
        if value not in (None, "", [], {}, False, 0) and text_contains_cart_signal(value):
            evidence.append(field)

    if evidence:
        return "yes", "公开接口返回商品/电商相关字段：" + "/".join(evidence)
    return "no", "公开接口未发现商品锚点或有效电商字段；不等于确定没有挂车。"


def has_truthy_pinned_marker(value: Any, *, path: str = "") -> tuple[bool, str]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            next_path = f"{path}.{key}" if path else str(key)
            if key_text in {"is_top", "is_pinned", "pinned", "stick_top"} and bool(child):
                return True, next_path
            if key_text in {"label_top_text", "top_text"} and child:
                return True, next_path
            found, evidence = has_truthy_pinned_marker(child, path=next_path)
            if found:
                return True, evidence
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found, evidence = has_truthy_pinned_marker(child, path=f"{path}[{index}]")
            if found:
                return True, evidence
    elif isinstance(value, str) and "置顶" in value:
        return True, path or "string_contains_置顶"
    return False, ""


def format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def normalize_structure(digg: int, comment: int, collect: int, share: int) -> str:
    if digg <= 0:
        return f"0 : {comment} : {collect} : {share}"
    return " : ".join(
        [
            "1",
            f"{comment / digg:.2f}",
            f"{collect / digg:.2f}",
            f"{share / digg:.2f}",
        ]
    )


def structure_note(digg: int, comment: int, collect: int, share: int) -> str:
    if digg <= 0 and comment <= 0 and collect <= 0 and share <= 0:
        return "点赞0，评论0，收藏0，分享0；无公开互动"
    if digg <= 0:
        return f"点赞0作为基准，评论{comment}，收藏{collect}，分享{share}；结构不可标准化"

    signals: list[str] = [f"点赞{digg}作为基准"]
    if comment >= digg * 0.3:
        signals.append(f"评论{comment}突出")
    elif comment >= digg * 0.12:
        signals.append(f"评论{comment}中等")
    elif comment >= digg * 0.08:
        signals.append(f"评论{comment}有一定参与")
    else:
        signals.append(f"评论{comment}偏低")

    if collect >= digg * 0.3:
        signals.append(f"收藏{collect}突出")
    elif collect >= digg * 0.12:
        signals.append(f"收藏{collect}中等")
    else:
        signals.append(f"收藏{collect}偏低")

    if share >= digg * 0.4:
        signals.append(f"分享{share}突出")
    elif share >= digg * 0.15:
        signals.append(f"分享{share}中等")
    else:
        signals.append(f"分享{share}偏低")

    return "，".join(signals)


def classify_structure(digg: int, comment: int, collect: int, share: int) -> str:
    if digg <= 0 and comment <= 0 and collect <= 0 and share <= 0:
        return "无互动型"
    if digg <= 0:
        return "非点赞基准型"

    strong: list[str] = []
    if comment >= digg * 0.3:
        strong.append("评论")
    if collect >= digg * 0.3:
        strong.append("收藏")
    if share >= digg * 0.4:
        strong.append("分享")
    if len(strong) >= 2:
        return "/".join(strong) + "复合型"
    if strong:
        return strong[0] + "型"
    return "点赞型"


def row_from_item(
    item: dict[str, Any],
    *,
    source: str,
    profile_position: int | None = None,
) -> dict[str, Any]:
    stats = item.get("statistics") or {}
    digg = safe_int(stats.get("digg_count"))
    comment = safe_int(stats.get("comment_count"))
    collect = safe_int(stats.get("collect_count"))
    share = safe_int(stats.get("share_count"))
    total = digg + comment + collect + share
    direct_pinned, direct_pinned_evidence = has_truthy_pinned_marker(item)
    cart_status, cart_evidence = cart_signal_from_item(item)

    create_time = safe_int(item.get("create_time"))
    return {
        "profile_position": profile_position if profile_position is not None else "",
        "aweme_id": str(item.get("aweme_id") or ""),
        "desc": str(item.get("desc") or "").replace("\n", " ").strip(),
        "create_time": datetime.fromtimestamp(create_time).isoformat() if create_time else "",
        "create_time_unix": create_time,
        "publish_hour": datetime.fromtimestamp(create_time).hour if create_time else "",
        "publish_time_bucket": publish_time_bucket(datetime.fromtimestamp(create_time)) if create_time else "",
        "publish_weekday": weekday_label(datetime.fromtimestamp(create_time)) if create_time else "",
        "is_weekend": "yes" if create_time and datetime.fromtimestamp(create_time).weekday() >= 5 else "no",
        "digg_count": digg,
        "comment_count": comment,
        "collect_count": collect,
        "share_count": share,
        "total_interaction": total,
        "interaction_structure_raw": f"{digg} : {comment} : {collect} : {share}",
        "interaction_structure_normalized": normalize_structure(digg, comment, collect, share),
        "structure_type": classify_structure(digg, comment, collect, share),
        "structure_note": structure_note(digg, comment, collect, share),
        "cart_status": cart_status,
        "cart_evidence": cart_evidence,
        "is_pinned": "yes" if direct_pinned else "unknown",
        "pinned_evidence": direct_pinned_evidence,
        "included_in_baseline": "no" if direct_pinned or cart_status == "yes" else "yes",
        "data_source": source,
        "data_confidence": "medium",
    }


def median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def mean(values: list[int]) -> float:
    return float(statistics.mean(values)) if values else 0.0


def quartile_stats(values: list[int]) -> dict[str, float]:
    sorted_values = sorted(values)
    if not sorted_values:
        return {"median": 0.0, "p25": 0.0, "p75": 0.0, "iqr": 0.0, "relative_iqr": 0.0}

    mid = len(sorted_values) // 2
    if len(sorted_values) >= 4:
        if len(sorted_values) % 2 == 0:
            lower = sorted_values[:mid]
            upper = sorted_values[mid:]
        else:
            lower = sorted_values[:mid]
            upper = sorted_values[mid + 1 :]
        p25 = median(lower)
        p75 = median(upper)
    else:
        p25 = float(min(sorted_values))
        p75 = float(max(sorted_values))

    med = median(sorted_values)
    iqr = p75 - p25
    if med > 0:
        relative_iqr = iqr / med
    else:
        relative_iqr = 0.0 if iqr == 0 else float("inf")
    return {"median": med, "p25": p25, "p75": p75, "iqr": iqr, "relative_iqr": relative_iqr}


def trimmed_mean(values: list[int]) -> tuple[float, int]:
    sorted_values = sorted(values)
    sample = len(sorted_values)
    if sample < 5:
        return mean(sorted_values), 0
    trim_count = max(1, int(sample * 0.2))
    if sample - trim_count * 2 < 3:
        trim_count = max(0, (sample - 3) // 2)
    trimmed_values = sorted_values[trim_count : sample - trim_count] if trim_count else sorted_values
    return mean(trimmed_values), trim_count


def format_ratio(value: float) -> str:
    if value == float("inf"):
        return "无限"
    return f"{value:.2f}"


def variation_level(relative_iqr: float) -> str:
    if relative_iqr == float("inf"):
        return "波动大"
    if relative_iqr <= 0.5:
        return "稳定"
    if relative_iqr <= 1.5:
        return "有波动"
    return "波动大"


def normal_level_judgment(name: str, median_value: float, trimmed_avg: float) -> str:
    if median_value <= 0:
        if trimmed_avg <= 0:
            return f"{name}中位数和去极值平均都为0，当前样本没有形成可读常态。"
        return f"{name}中位数为0，但去极值平均为{format_number(trimmed_avg)}，说明只有少数作品有这个动作，不能按去极值平均预估常态。"
    ratio = trimmed_avg / median_value
    median_text = format_number(median_value)
    trimmed_text = format_number(trimmed_avg)

    if name == "点赞":
        if ratio > 1.2:
            return (
                f"点赞常态先按{median_text}赞附近看，不要按{trimmed_text}赞预估；"
                "高赞作品需要单独拎出来拆选题和发布时间。"
            )
        if ratio < 0.8:
            return (
                f"点赞中位数{median_text}高于去极值平均{trimmed_text}，说明中间层作品比两端样本更好；"
                "常态仍按中位数附近看。"
            )
        return f"点赞中位数{median_text}、去极值平均{trimmed_text}接近，正常一条作品大致可以按这个区间看。"

    if name == "评论":
        if ratio > 1.2:
            return (
                f"评论不能直接按{trimmed_text}条看，少数高评论作品把数值抬高；"
                f"普通作品先按{median_text}条左右理解。"
            )
        if ratio < 0.8:
            return (
                f"评论中位数{median_text}高于去极值平均{trimmed_text}，说明低评论作品在拖后腿；"
                "后续要看低评论作品是不是选题不容易开口。"
            )
        return f"评论大致稳定在{median_text}条上下，说明普通作品也有基本评论参与。"

    if name == "收藏":
        if ratio > 1.2:
            return (
                f"收藏常态先按{median_text}次附近看，{trimmed_text}次不能当每条都能达到；"
                "高收藏作品要单独看是不是清单、教程、商品推荐或强场景标题。"
            )
        if ratio < 0.8:
            return (
                f"收藏中位数{median_text}高于去极值平均{trimmed_text}，说明低收藏作品拉低整体；"
                "后续要看哪些内容不值得用户存。"
            )
        return f"收藏大致在{median_text}次上下，普通作品也有人愿意存下来。"

    if name == "分享":
        if ratio > 1.2:
            return (
                f"分享常态先按{median_text}次附近看，{trimmed_text}次不是每条作品都有；"
                "高分享作品要单独看是不是踩中了节日、情绪、亲友转发或实用提醒。"
            )
        if ratio < 0.8:
            return (
                f"分享中位数{median_text}高于去极值平均{trimmed_text}，说明低分享作品拉低整体；"
                "后续要看哪些内容没有转发理由。"
            )
        return f"分享大致在{median_text}次上下，普通作品也有人愿意转给别人。"

    if 0.8 <= ratio <= 1.2:
        return f"{name}中位数{median_text}、去极值平均{trimmed_text}接近，常态可以按这个区间看。"
    return f"{name}常态先按中位数{median_text}看，去极值平均{trimmed_text}只作参考。"


def format_days(value: float) -> str:
    if value <= 0:
        return "0天"
    if value < 1:
        return f"{value * 24:.1f}小时"
    return f"{value:.1f}天"


def format_datetime_from_unix(value: int) -> str:
    return datetime.fromtimestamp(value).isoformat() if value else ""


def publish_time_bucket(dt: datetime) -> str:
    hour = dt.hour
    if 5 <= hour < 11:
        return "上午"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜/凌晨"


def weekday_label(dt: datetime) -> str:
    labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return labels[dt.weekday()]


def format_count_dict(counter: dict[str, int]) -> str:
    if not counter:
        return "样本不足"
    return "；".join(f"{key}{value}条" for key, value in sorted(counter.items(), key=lambda item: -item[1]))


def publish_timing_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dated_rows: list[tuple[dict[str, Any], datetime]] = []
    for row in rows:
        create_time = safe_int(row.get("create_time_unix"))
        if create_time:
            dated_rows.append((row, datetime.fromtimestamp(create_time)))

    if not dated_rows:
        return {
            "publish_time_bucket_distribution": "样本不足",
            "publish_weekday_distribution": "样本不足",
            "publish_weekend_count": 0,
            "publish_weekday_count": 0,
            "high_sample_publish_timing": "样本不足",
            "publish_timing_judgment": "样本发布时间不足，无法判断具体发布时间段。",
        }

    bucket_counts: dict[str, int] = {}
    weekday_counts: dict[str, int] = {}
    weekend_count = 0
    for _, dt in dated_rows:
        bucket = publish_time_bucket(dt)
        weekday = weekday_label(dt)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        weekday_counts[weekday] = weekday_counts.get(weekday, 0) + 1
        if dt.weekday() >= 5:
            weekend_count += 1

    sorted_rows = sorted(
        dated_rows,
        key=lambda item: safe_int(item[0].get("digg_count")),
        reverse=True,
    )
    high_count = min(3, len(sorted_rows))
    high_rows = sorted_rows[:high_count]
    high_parts = [
        f"{publish_time_bucket(dt)}{dt.hour}点/{weekday_label(dt)}，点赞{safe_int(row.get('digg_count'))}"
        for row, dt in high_rows
    ]
    top_bucket, top_bucket_count = max(bucket_counts.items(), key=lambda item: item[1])
    high_bucket_counts: dict[str, int] = {}
    for _, dt in high_rows:
        bucket = publish_time_bucket(dt)
        high_bucket_counts[bucket] = high_bucket_counts.get(bucket, 0) + 1
    high_top_bucket, high_top_count = max(high_bucket_counts.items(), key=lambda item: item[1])

    if high_top_count >= 2:
        high_sample_text = f"点赞最高的{high_count}条里，{high_top_bucket}出现{high_top_count}条"
    else:
        high_sample_text = f"点赞最高的{high_count}条分散在不同时间段"

    timing_judgment = (
        f"普通作品发布最多集中在{top_bucket}（{top_bucket_count}条）；"
        f"{high_sample_text}。"
        "这只能说明高位样本的发布时间分布，不能单独证明哪个时间段一定更好。"
    )

    return {
        "publish_time_bucket_distribution": format_count_dict(bucket_counts),
        "publish_weekday_distribution": format_count_dict(weekday_counts),
        "publish_weekend_count": weekend_count,
        "publish_weekday_count": len(dated_rows) - weekend_count,
        "high_sample_publish_timing": "；".join(high_parts),
        "publish_timing_judgment": timing_judgment,
    }


def publishing_frequency_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    times = sorted(
        [safe_int(row.get("create_time_unix")) for row in rows if safe_int(row.get("create_time_unix"))],
        reverse=True,
    )
    if len(times) < 2:
        return {
            "publish_time_span_days": 0,
            "publish_interval_median_days": 0,
            "publish_interval_avg_days": 0,
            "publish_interval_max_days": 0,
            "latest_publish_time": format_datetime_from_unix(times[0]) if times else "",
            "oldest_publish_time": format_datetime_from_unix(times[-1]) if times else "",
            "publish_frequency_judgment": "样本发布时间不足，无法判断发布频率。",
        }

    intervals_days = [
        (times[index] - times[index + 1]) / 86400 for index in range(len(times) - 1)
    ]
    span_days = (times[0] - times[-1]) / 86400
    median_interval = median([int(round(value * 100)) for value in intervals_days]) / 100
    avg_interval = mean([int(round(value * 100)) for value in intervals_days]) / 100
    max_interval = max(intervals_days)

    if median_interval <= 2:
        cadence = "发布频率较高"
    elif median_interval <= 5:
        cadence = "发布频率中等"
    else:
        cadence = "发布频率偏低"

    if max_interval >= median_interval * 3 and max_interval >= 5:
        stability = "中间存在明显断更/间隔拉长"
    else:
        stability = "发布节奏相对连续"

    judgment = (
        f"近{len(times)}条普通作品覆盖{format_days(span_days)}，"
        f"相邻发布中位间隔{format_days(median_interval)}，平均间隔{format_days(avg_interval)}，"
        f"最大间隔{format_days(max_interval)}；{cadence}，{stability}。"
    )
    return {
        "publish_time_span_days": round(span_days, 2),
        "publish_interval_median_days": round(median_interval, 2),
        "publish_interval_avg_days": round(avg_interval, 2),
        "publish_interval_max_days": round(max_interval, 2),
        "latest_publish_time": format_datetime_from_unix(times[0]),
        "oldest_publish_time": format_datetime_from_unix(times[-1]),
        "publish_frequency_judgment": judgment,
    }


def account_problem_judgment(
    *,
    digg_median: float,
    digg_trimmed_avg: float,
    digg_relative_iqr: float,
    comment_median: float,
    comment_trimmed_avg: float,
    comment_relative_iqr: float,
    collect_median: float,
    collect_trimmed_avg: float,
    collect_relative_iqr: float,
    share_median: float,
    share_trimmed_avg: float,
    share_relative_iqr: float,
    publish_frequency: str,
) -> str:
    problems: list[str] = []

    if digg_median > 0 and (digg_trimmed_avg / digg_median > 1.2 or digg_relative_iqr > 1.5):
        problems.append(
            "点赞起伏大，账号不是每条都有稳定点赞，更像少数选题、节点或爆款内容把点赞拉高"
        )
    else:
        problems.append("点赞常态相对清楚，正常一条作品大概能拿多少赞比较容易估")

    if comment_median > 0 and 0.8 <= comment_trimmed_avg / comment_median <= 1.2:
        if comment_relative_iqr <= 1.5:
            problems.append(
                "评论比点赞更稳，说明粉丝参与有基本盘；但是否是有效提问，还要看评论内容"
            )
        else:
            problems.append(
                "评论常态数值接近，但分布仍有波动，需要看高评论作品是不是由争议或强提问带起"
            )
    else:
        problems.append("评论常态不稳，不能直接判断粉丝讨论能力强")

    collect_unstable = collect_median > 0 and (
        collect_trimmed_avg / collect_median > 1.2 or collect_relative_iqr > 1.5
    )
    share_unstable = share_median > 0 and (
        share_trimmed_avg / share_median > 1.2 or share_relative_iqr > 1.5
    )
    if collect_unstable and share_unstable:
        problems.append(
            "有些内容能让粉丝收藏或分享，但不是每条都能做到，说明粉丝只会对特定选题或具体标题有反应"
        )
    elif collect_unstable:
        problems.append("收藏波动明显，说明不是每条内容都值得粉丝存下来以后看")
    elif share_unstable:
        problems.append("分享波动明显，说明不是每条内容都值得粉丝转给别人")
    else:
        problems.append("收藏和分享相对稳定，粉丝比较愿意把内容存下来或转给别人")

    if "发布频率中等" in publish_frequency or "发布频率较高" in publish_frequency:
        problems.append("发布节奏还在持续，互动常态有近期参考价值")
    elif "发布频率偏低" in publish_frequency:
        problems.append("发布频率偏低，互动常态可能受更新不连续影响")

    return "；".join(problems)


def cooperation_data_reminder(
    *,
    digg_median: float,
    digg_trimmed_avg: float,
    digg_relative_iqr: float,
    avg_total: float,
    median_total: float,
    max_total: int,
) -> str:
    high_sample_risk = median_total > 0 and max_total / median_total > 5
    digg_unstable = digg_median > 0 and (
        digg_trimmed_avg / digg_median > 1.2 or digg_relative_iqr > 1.5
    )

    if high_sample_risk or digg_unstable:
        return (
            "后面如果要评估合作，别拿她最火那条或平均数当预期。"
            "这个账号不是每条都稳，漂亮数据主要靠少数几条撑起来；"
            "下一步要先把这些高互动内容拎出来，看它们到底为什么高。"
        )

    return (
        "后面如果要评估合作，可以参考她的常态数据，但第一项仍不判断能不能合作；"
        "下一步仍要看高互动内容和评论，确认哪些内容是真被粉丝接住了。"
    )


def annotate_pinned_rows(rows: list[dict[str, Any]]) -> None:
    profile_rows = [row for row in rows if row.get("profile_position") != ""]
    if len(profile_rows) < 5:
        return

    # Pinned works usually appear before newer non-pinned works. If a top card is
    # older than later cards, it should not represent the creator's current baseline.
    for index, row in enumerate(profile_rows[:3]):
        if row.get("is_pinned") == "yes":
            continue
        create_time = safe_int(row.get("create_time_unix"))
        if not create_time:
            continue
        later_times = [safe_int(item.get("create_time_unix")) for item in profile_rows[index + 1 :]]
        later_times = [value for value in later_times if value]
        if later_times and max(later_times) > create_time + 86400:
            row["is_pinned"] = "likely"
            row["pinned_evidence"] = "chronology_top_card_older_than_later_newer_work"
            row["included_in_baseline"] = "no"


def baseline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("included_in_baseline") == "yes"]


def homepage_scan_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("profile_position") != ""
        and safe_int(row.get("profile_position")) <= HOMEPAGE_CART_SCAN_COUNT
    ]


def pinned_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("is_pinned") in {"yes", "likely"}]


def cart_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("cart_status") == "yes" and row.get("is_pinned") not in {"yes", "likely"}
    ]


def homepage_cart_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scan_rows = homepage_scan_rows(rows)
    if not scan_rows and rows:
        scan_rows = rows
    return cart_rows(scan_rows)


def trim_rows_to_baseline_target(rows: list[dict[str, Any]], target_count: int) -> list[dict[str, Any]]:
    trimmed: list[dict[str, Any]] = []
    included_count = 0
    for row in rows:
        if row.get("included_in_baseline") == "yes":
            if included_count >= target_count:
                continue
            included_count += 1
            trimmed.append(row)
        else:
            trimmed.append(row)
    return trimmed


POSTS_CSV_COLUMNS = [
    ("profile_position", "主页位置"),
    ("aweme_id", "作品ID"),
    ("desc", "作品标题"),
    ("create_time", "发布时间"),
    ("publish_hour", "发布小时"),
    ("publish_time_bucket", "发布时间段"),
    ("publish_weekday", "发布周几"),
    ("is_weekend", "是否周末"),
    ("digg_count", "点赞数"),
    ("comment_count", "评论数"),
    ("collect_count", "收藏数"),
    ("share_count", "分享数"),
    ("total_interaction", "总互动"),
    ("interaction_structure_raw", "赞评藏转原始结构"),
    ("interaction_structure_normalized", "赞评藏转标准结构"),
    ("structure_type", "结构类型"),
    ("structure_note", "结构判断"),
    ("cart_status", "是否发现挂车"),
    ("cart_evidence", "挂车证据"),
    ("data_source", "数据来源"),
    ("data_confidence", "数据可信度"),
]

SUMMARY_CSV_COLUMNS = [
    ("raw_sample_count", "原始采集作品数"),
    ("target_baseline_count", "目标普通作品样本数"),
    ("baseline_sample_count", "进入基础盘作品数"),
    ("excluded_pinned_count", "排除置顶作品数"),
    ("excluded_cart_count", "排除挂车作品数"),
    ("latest_publish_time", "最近发布时间"),
    ("oldest_publish_time", "最早样本发布时间"),
    ("publish_time_span_days", "样本发布时间跨度天数"),
    ("publish_interval_median_days", "发布间隔中位天数"),
    ("publish_interval_avg_days", "发布间隔平均天数"),
    ("publish_interval_max_days", "最大发布间隔天数"),
    ("publish_frequency_judgment", "发布频率判断"),
    ("publish_time_bucket_distribution", "发布时间段分布"),
    ("publish_weekday_distribution", "发布周几分布"),
    ("publish_weekday_count", "工作日发布数"),
    ("publish_weekend_count", "周末发布数"),
    ("high_sample_publish_timing", "高位样本发布时间"),
    ("publish_timing_judgment", "具体发布时间判断"),
    ("cart_scan_count", "挂车扫描作品数"),
    ("cart_video_count", "发现挂车视频数"),
    ("cart_video_titles", "发现挂车视频标题"),
    ("cart_video_judgment", "挂车视频判定"),
    ("cart_video_mode", "挂车视频模式"),
    ("total_interaction_avg", "总互动平均值"),
    ("total_interaction_median", "总互动中位数"),
    ("total_interaction_max", "最高总互动"),
    ("total_interaction_min", "最低总互动"),
    ("avg_to_median", "平均/中位"),
    ("max_to_median", "最高/中位"),
    ("mean_representativeness", "均值代表性"),
    ("mean_representativeness_reason", "均值代表性原因"),
    ("digg_median", "点赞中位数"),
    ("digg_trimmed_mean", "点赞去极值平均"),
    ("digg_trim_count", "点赞去极值数量"),
    ("digg_max", "点赞高位样本"),
    ("digg_min", "点赞低位样本"),
    ("digg_p25", "点赞P25"),
    ("digg_p75", "点赞P75"),
    ("digg_relative_iqr", "点赞相对波动"),
    ("comment_median", "评论中位数"),
    ("comment_trimmed_mean", "评论去极值平均"),
    ("comment_trim_count", "评论去极值数量"),
    ("comment_max", "评论高位样本"),
    ("comment_min", "评论低位样本"),
    ("comment_p25", "评论P25"),
    ("comment_p75", "评论P75"),
    ("comment_relative_iqr", "评论相对波动"),
    ("collect_median", "收藏中位数"),
    ("collect_trimmed_mean", "收藏去极值平均"),
    ("collect_trim_count", "收藏去极值数量"),
    ("collect_max", "收藏高位样本"),
    ("collect_min", "收藏低位样本"),
    ("collect_p25", "收藏P25"),
    ("collect_p75", "收藏P75"),
    ("collect_relative_iqr", "收藏相对波动"),
    ("share_median", "分享中位数"),
    ("share_trimmed_mean", "分享去极值平均"),
    ("share_trim_count", "分享去极值数量"),
    ("share_max", "分享高位样本"),
    ("share_min", "分享低位样本"),
    ("share_p25", "分享P25"),
    ("share_p75", "分享P75"),
    ("share_relative_iqr", "分享相对波动"),
    ("normal_level_judgment", "四项常态校验"),
    ("most_stable_dimension", "变动最小维度"),
    ("most_volatile_dimension", "变动最大维度"),
    ("dimension_variation_judgment", "四项波动判断"),
    ("account_problem_judgment", "账号问题判断"),
    ("cooperation_data_reminder", "数据层面的合作提醒"),
    ("median_structure_raw", "中位赞评藏转原始结构"),
    ("median_structure_normalized", "中位赞评藏转标准结构"),
    ("median_structure_note", "中位结构判断"),
    ("structure_consistency", "结构稳定性"),
    ("base_interaction_tier", "基础互动档位"),
]


def humanize_cell(value: Any) -> Any:
    mapping = {
        "yes": "是",
        "no": "否",
        "unknown": "未知",
        "likely": "疑似",
        "low": "低",
        "medium": "中",
        "high": "高",
        "mobile_share_ssr": "移动分享页公开数据",
        "web_profile_post_api": "主页作品公开接口",
        "is_top": "接口标记置顶",
        "chronology_top_card_older_than_later_newer_work": "主页前排作品早于后续更新，疑似置顶",
    }
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def localized_row(row: dict[str, Any], columns: list[tuple[str, str]]) -> dict[str, Any]:
    return {label: humanize_cell(row.get(key, "")) for key, label in columns}


def representation_label(sample: int, avg_total: float, median_total: float, max_total: int) -> tuple[str, str]:
    if sample < 5:
        return "样本不足", "样本少于5条，不能判断平均值是否代表账号常态。"
    if median_total <= 0:
        return "低", "中位互动为0，平均值不适合作为账号常态。"

    avg_median = avg_total / median_total
    max_median = max_total / median_total
    if avg_median <= 1.3 and max_median <= 2:
        return "高", "平均值接近中位数，最高值没有明显拉高整体，能代表账号常态。"
    if avg_median <= 2 and max_median <= 5:
        return "中", "平均值有一定波动，需要结合中位数和最高值一起看。"
    return "低", "平均值明显被高互动作品拉高，应优先按中位数判断账号常态。"


def base_tier(sample: int, median_total: float) -> str:
    if sample < 5:
        return "样本不足"
    if median_total < 100:
        return "低"
    if median_total < 500:
        return "中"
    return "高"


def structure_consistency(rows: list[dict[str, Any]]) -> str:
    if len(rows) < 5:
        return "样本不足"
    types = [str(row.get("structure_type") or "") for row in rows]
    most_common = max(set(types), key=types.count)
    share = types.count(most_common) / len(types)
    if share >= 0.7:
        return f"稳定：{most_common}"
    if share >= 0.4:
        return f"有波动：{most_common}占比较高"
    return "波动较大"


def cart_video_stats(rows: list[dict[str, Any]], *, scan_label: str = "主页前20条") -> dict[str, Any]:
    scan_rows = [row for row in rows if row.get("is_pinned") not in {"yes", "likely"}]
    sample = len(scan_rows)
    found_cart_rows = cart_rows(scan_rows)
    cart_count = len(found_cart_rows)
    titles = [str(row.get("desc") or "") for row in found_cart_rows[:3]]
    title_text = "；".join(titles)
    cart_only = sample > 0 and cart_count == sample
    if sample <= 0:
        judgment = f"{scan_label}里没有可判断的非置顶作品，无法判断是否有挂车短视频。"
        mode = "样本不足"
    elif cart_only:
        judgment = (
            f"{scan_label}非置顶作品共{sample}条，公开接口发现全部都是挂车/商品锚点视频。"
            "主页普通短视频基础盘没有单独分析必要，应直接分析挂车短视频维度。"
        )
        mode = "全是挂车短视频"
    elif cart_count:
        judgment = (
            f"{scan_label}非置顶作品中，公开接口发现{cart_count}条挂车/商品锚点视频。"
            "这些视频不进入主页普通短视频基础盘，单独进入挂车短视频维度。"
        )
        mode = "部分挂车短视频"
    else:
        judgment = (
            f"{scan_label}非置顶作品中，公开接口未发现挂车/商品锚点字段；"
            "这只能说明本次公开字段未发现，不等于绝对没有挂车。"
        )
        mode = "未发现挂车短视频"
    return {
        "cart_scan_label": scan_label,
        "cart_scan_count": sample,
        "cart_video_count": cart_count,
        "cart_video_titles": title_text,
        "cart_video_judgment": judgment,
        "cart_video_mode": mode,
        "cart_only_top20": "yes" if cart_only else "no",
    }


def analysis_route(rows: list[dict[str, Any]]) -> dict[str, Any]:
    normal_count = len(baseline_rows(rows))
    cart_count = len(homepage_cart_rows(rows))
    scan_rows = homepage_scan_rows(rows)
    scan_label = f"主页前{HOMEPAGE_CART_SCAN_COUNT}条"
    if not scan_rows and rows:
        scan_rows = rows
        scan_label = "当前视频"
    cart_stats = cart_video_stats(scan_rows, scan_label=scan_label)

    if normal_count and cart_count:
        mode = "mixed"
        judgment = "本次同时发现主页普通作品和挂车作品，两个样本池分别生成分析，不混算。"
    elif cart_count:
        mode = "cart_only"
        judgment = "本次没有可进入普通主页基础盘的非置顶、非挂车作品，只生成挂车作品分析。"
    elif normal_count:
        mode = "normal_only"
        judgment = "本次未发现可单独分析的挂车作品，只生成主页普通作品分析。"
    else:
        mode = "empty"
        judgment = "本次没有可进入普通主页基础盘或挂车基础盘的有效作品。"

    return {
        "mode": mode,
        "normal_sample_count": normal_count,
        "cart_sample_count": cart_count,
        "route_judgment": judgment,
        **cart_stats,
    }


def build_summary(
    rows: list[dict[str, Any]], *, target_baseline_count: int, scope: str = "homepage"
) -> dict[str, Any]:
    if scope == "cart":
        usable_rows = [row for row in rows if row.get("is_pinned") not in {"yes", "likely"}]
        cart_stats = cart_video_stats(usable_rows)
        excluded_pinned_count = len(pinned_rows(rows))
        excluded_cart_count = 0
    else:
        usable_rows = baseline_rows(rows)
        scan_rows = homepage_scan_rows(rows)
        scan_label = f"主页前{HOMEPAGE_CART_SCAN_COUNT}条"
        if not scan_rows and rows:
            scan_rows = rows
            scan_label = "当前视频"
        cart_stats = cart_video_stats(scan_rows, scan_label=scan_label)
        excluded_pinned_count = len(pinned_rows(rows))
        excluded_cart_count = len(cart_rows(rows))
    frequency = publishing_frequency_stats(usable_rows)
    timing = publish_timing_stats(usable_rows)
    totals = [safe_int(row["total_interaction"]) for row in usable_rows]
    comments = [safe_int(row["comment_count"]) for row in usable_rows]
    collects = [safe_int(row["collect_count"]) for row in usable_rows]
    shares = [safe_int(row["share_count"]) for row in usable_rows]
    diggs = [safe_int(row["digg_count"]) for row in usable_rows]
    sample = len(usable_rows)
    avg_total = mean(totals)
    median_total = median(totals)
    max_total = max(totals) if totals else 0
    min_total = min(totals) if totals else 0
    avg_median_ratio = round(avg_total / median_total, 2) if median_total else 0
    max_median_ratio = round(max_total / median_total, 2) if median_total else 0
    median_digg = median(diggs)
    median_comment = median(comments)
    median_collect = median(collects)
    median_share = median(shares)
    digg_trimmed_avg, digg_trim_count = trimmed_mean(diggs)
    comment_trimmed_avg, comment_trim_count = trimmed_mean(comments)
    collect_trimmed_avg, collect_trim_count = trimmed_mean(collects)
    share_trimmed_avg, share_trim_count = trimmed_mean(shares)
    dimension_stats = {
        "点赞": quartile_stats(diggs),
        "评论": quartile_stats(comments),
        "收藏": quartile_stats(collects),
        "分享": quartile_stats(shares),
    }
    stable_name, stable_stats = min(
        dimension_stats.items(), key=lambda item: item[1]["relative_iqr"]
    )
    volatile_name, volatile_stats = max(
        dimension_stats.items(), key=lambda item: item[1]["relative_iqr"]
    )
    stable_dimension = (
        f"{stable_name}（相对波动{format_ratio(stable_stats['relative_iqr'])}，"
        f"{variation_level(stable_stats['relative_iqr'])}）"
    )
    volatile_dimension = (
        f"{volatile_name}（相对波动{format_ratio(volatile_stats['relative_iqr'])}，"
        f"{variation_level(volatile_stats['relative_iqr'])}）"
    )
    dimension_variation_judgment = (
        f"四项分开看，变动最小的是{stable_dimension}；变动最大的是{volatile_dimension}。"
    )
    digg_normal_judgment = normal_level_judgment("点赞", median_digg, digg_trimmed_avg)
    comment_normal_judgment = normal_level_judgment("评论", median_comment, comment_trimmed_avg)
    collect_normal_judgment = normal_level_judgment("收藏", median_collect, collect_trimmed_avg)
    share_normal_judgment = normal_level_judgment("分享", median_share, share_trimmed_avg)
    normal_judgments = [
        digg_normal_judgment,
        comment_normal_judgment,
        collect_normal_judgment,
        share_normal_judgment,
    ]
    normal_level_summary = "；".join(normal_judgments)
    problem_judgment = account_problem_judgment(
        digg_median=median_digg,
        digg_trimmed_avg=digg_trimmed_avg,
        digg_relative_iqr=dimension_stats["点赞"]["relative_iqr"],
        comment_median=median_comment,
        comment_trimmed_avg=comment_trimmed_avg,
        comment_relative_iqr=dimension_stats["评论"]["relative_iqr"],
        collect_median=median_collect,
        collect_trimmed_avg=collect_trimmed_avg,
        collect_relative_iqr=dimension_stats["收藏"]["relative_iqr"],
        share_median=median_share,
        share_trimmed_avg=share_trimmed_avg,
        share_relative_iqr=dimension_stats["分享"]["relative_iqr"],
        publish_frequency=str(frequency.get("publish_frequency_judgment", "")),
    )
    cooperation_reminder = cooperation_data_reminder(
        digg_median=median_digg,
        digg_trimmed_avg=digg_trimmed_avg,
        digg_relative_iqr=dimension_stats["点赞"]["relative_iqr"],
        avg_total=avg_total,
        median_total=median_total,
        max_total=max_total,
    )
    representativeness, representativeness_reason = representation_label(
        sample, avg_total, median_total, max_total
    )

    median_structure_raw = (
        f"{format_number(median_digg)} : {format_number(median_comment)} : "
        f"{format_number(median_collect)} : {format_number(median_share)}"
    )
    median_structure_normalized = normalize_structure(
        int(round(median_digg)),
        int(round(median_comment)),
        int(round(median_collect)),
        int(round(median_share)),
    )
    median_structure_note = structure_note(
        int(round(median_digg)),
        int(round(median_comment)),
        int(round(median_collect)),
        int(round(median_share)),
    )

    return {
        "raw_sample_count": len(rows),
        "target_baseline_count": target_baseline_count,
        "baseline_sample_count": sample,
        "excluded_pinned_count": excluded_pinned_count,
        "excluded_cart_count": excluded_cart_count,
        **frequency,
        **timing,
        **cart_stats,
        "total_interaction_avg": round(avg_total, 2),
        "total_interaction_median": round(median_total, 2),
        "total_interaction_max": max_total,
        "total_interaction_min": min_total,
        "avg_to_median": avg_median_ratio,
        "max_to_median": max_median_ratio,
        "mean_representativeness": representativeness,
        "mean_representativeness_reason": representativeness_reason,
        "digg_median": round(median_digg, 2),
        "digg_trimmed_mean": round(digg_trimmed_avg, 2),
        "digg_trim_count": digg_trim_count,
        "digg_max": max(diggs) if diggs else 0,
        "digg_min": min(diggs) if diggs else 0,
        "digg_p25": round(dimension_stats["点赞"]["p25"], 2),
        "digg_p75": round(dimension_stats["点赞"]["p75"], 2),
        "digg_relative_iqr": format_ratio(dimension_stats["点赞"]["relative_iqr"]),
        "comment_median": round(median_comment, 2),
        "comment_trimmed_mean": round(comment_trimmed_avg, 2),
        "comment_trim_count": comment_trim_count,
        "comment_max": max(comments) if comments else 0,
        "comment_min": min(comments) if comments else 0,
        "comment_p25": round(dimension_stats["评论"]["p25"], 2),
        "comment_p75": round(dimension_stats["评论"]["p75"], 2),
        "comment_relative_iqr": format_ratio(dimension_stats["评论"]["relative_iqr"]),
        "collect_median": round(median_collect, 2),
        "collect_trimmed_mean": round(collect_trimmed_avg, 2),
        "collect_trim_count": collect_trim_count,
        "collect_max": max(collects) if collects else 0,
        "collect_min": min(collects) if collects else 0,
        "collect_p25": round(dimension_stats["收藏"]["p25"], 2),
        "collect_p75": round(dimension_stats["收藏"]["p75"], 2),
        "collect_relative_iqr": format_ratio(dimension_stats["收藏"]["relative_iqr"]),
        "share_median": round(median_share, 2),
        "share_trimmed_mean": round(share_trimmed_avg, 2),
        "share_trim_count": share_trim_count,
        "share_max": max(shares) if shares else 0,
        "share_min": min(shares) if shares else 0,
        "share_p25": round(dimension_stats["分享"]["p25"], 2),
        "share_p75": round(dimension_stats["分享"]["p75"], 2),
        "share_relative_iqr": format_ratio(dimension_stats["分享"]["relative_iqr"]),
        "normal_level_judgment": normal_level_summary,
        "digg_normal_judgment": digg_normal_judgment,
        "comment_normal_judgment": comment_normal_judgment,
        "collect_normal_judgment": collect_normal_judgment,
        "share_normal_judgment": share_normal_judgment,
        "most_stable_dimension": stable_dimension,
        "most_volatile_dimension": volatile_dimension,
        "dimension_variation_judgment": dimension_variation_judgment,
        "account_problem_judgment": problem_judgment,
        "cooperation_data_reminder": cooperation_reminder,
        "median_structure_raw": median_structure_raw,
        "median_structure_normalized": median_structure_normalized,
        "median_structure_note": median_structure_note,
        "structure_consistency": structure_consistency(usable_rows),
        "base_interaction_tier": base_tier(sample, median_total),
    }


def preliminary_account_judgment(summary: dict[str, Any], *, scope: str = "homepage") -> list[str]:
    sample = safe_int(summary.get("baseline_sample_count", summary.get("sample_count", 0)))
    tier = str(summary.get("base_interaction_tier", "样本不足"))
    median_note = str(summary.get("median_structure_note", ""))
    consistency = str(summary.get("structure_consistency", "样本不足"))
    dimension_variation = str(summary.get("dimension_variation_judgment", ""))
    publish_frequency = str(summary.get("publish_frequency_judgment", ""))
    cart_only_top20 = str(summary.get("cart_only_top20", "no")) == "yes"
    sample_name = "挂车短视频" if scope == "cart" else "普通作品"
    if scope == "cart":
        publish_frequency = publish_frequency.replace("普通作品", "挂车短视频")

    if sample < 5:
        if cart_only_top20:
            return [
                f"- 初判：主页前{HOMEPAGE_CART_SCAN_COUNT}条非置顶作品全部是挂车短视频，主页普通短视频基础盘不单独判断。",
                "- 这类账号当前应直接看挂车短视频维度：挂的是什么、互动怎么样、粉丝在评论里问不问购买/适用问题。",
            ]
        return [
            f"- 初判：当前只有{sample}条{sample_name}进入基础盘，样本不足，不能判断常态。",
            f"- 可读信号：中位结构为{summary['median_structure_raw']}；{median_note}。",
            "- 本项只保留样本不足结论，不延伸判断账号是否值得继续分析。",
        ]

    structure_judgment = f"互动结构初判：{median_note}。"
    if "评论" in median_note:
        if re.search(r"评论\d+偏低", median_note):
            structure_judgment += " 账号不是强讨论型，评论参与不是主要优势。"
        elif re.search(r"评论\d+有一定参与", median_note):
            structure_judgment += " 评论不算低，但还没到强讨论型，后续要看评论内容是不是有效提问。"
        elif re.search(r"评论\d+突出", median_note):
            structure_judgment += " 账号有较强讨论/参与信号，后续值得优先看评论内容质量。"
    if re.search(r"收藏\d+突出|分享\d+突出", median_note):
        structure_judgment += " 收藏或分享相对突出，说明这类内容有人愿意存下来或转给别人。"
    elif re.search(r"收藏\d+中等|分享\d+中等", median_note):
        structure_judgment += " 收藏或分享不差，但还没到每条都能让人存、让人转的程度。"

    return [
        f"- 初判：账号基础互动档位为{tier}。",
        f"- 四项波动：{dimension_variation}",
        f"- 发布频率：{publish_frequency}",
        f"- {structure_judgment}",
        f"- 结构稳定性：{consistency}。",
    ]


def build_analysis(
    rows: list[dict[str, Any]], target: str, summary: dict[str, Any], *, scope: str = "homepage"
) -> str:
    raw_sample = safe_int(summary.get("raw_sample_count", summary.get("sample_count", len(rows))))
    target_baseline_count = safe_int(summary.get("target_baseline_count", raw_sample))
    baseline_sample = safe_int(summary.get("baseline_sample_count", summary.get("sample_count", len(rows))))
    is_cart_scope = scope == "cart"
    title = "挂车短视频公开互动基础盘" if is_cart_scope else "账号公开互动基础盘"
    target_label = "目标挂车视频样本数" if is_cart_scope else "目标普通作品样本数"
    baseline_label = "进入挂车基础盘样本数" if is_cart_scope else "进入基础盘计算样本数"
    sample_label = "挂车短视频" if is_cart_scope else "普通作品"
    data_scope = (
        "挂车短视频基础盘只看主页前20条里发现的非置顶挂车作品；播放量不进入表格和判断。"
        if is_cart_scope
        else "主页普通短视频基础盘只看非置顶、非挂车作品；播放量不进入表格和判断。"
    )

    def scoped_text(value: Any) -> str:
        text = str(value or "")
        if is_cart_scope:
            text = text.replace("普通作品", "挂车短视频")
            text = text.replace("正常一条作品", "正常一条挂车短视频")
        return text

    common_header = [
        f"# {title}",
        "",
        f"- 输入：`{target}`",
        f"- 原始样本数：{raw_sample}",
        f"- {target_label}：{target_baseline_count}",
        f"- {baseline_label}：{baseline_sample}",
        f"- 排除置顶/疑似置顶数：{summary.get('excluded_pinned_count', 0)}",
        f"- 排除挂车视频数：{summary.get('excluded_cart_count', 0)}",
        f"- 数据口径：{data_scope}",
    ]

    if not is_cart_scope and str(summary.get("cart_only_top20", "no")) == "yes":
        return "\n".join(
            [
                *common_header,
                "",
                "## 1. 初步账号判断",
                "",
                *preliminary_account_judgment(summary, scope=scope),
                "",
                "## 2. 挂车短视频判定",
                "",
                f"- {summary.get('cart_scan_label', f'主页前{HOMEPAGE_CART_SCAN_COUNT}条')}扫描作品数：{summary.get('cart_scan_count', 0)}",
                f"- 发现挂车短视频数：{summary.get('cart_video_count', 0)}",
                f"- 发现挂车短视频标题：{summary.get('cart_video_titles', '') or '无'}",
                f"- 判定：{summary.get('cart_video_judgment', '')}",
                "",
                "## 3. 下一步",
                "",
                "- 不再硬凑主页普通短视频基础盘；直接进入挂车短视频分析，看挂车内容主题、商品信号、互动数据和评论里的购买/适用问题。",
            ]
        )

    return "\n".join(
        [
            *common_header,
            "",
            "## 1. 初步账号判断",
            "",
            *preliminary_account_judgment(summary, scope=scope),
            "",
            "## 2. 数据背后的账号问题",
            "",
            f"- {summary.get('account_problem_judgment', '')}",
            "",
            "## 3. 数据层面的合作提醒",
            "",
            f"- {summary.get('cooperation_data_reminder', '')}",
            "",
            "## 4. 样本说明" if is_cart_scope else "## 4. 挂车视频判定",
            "",
            *(
                [
                    f"- 当前分析对象：主页前{HOMEPAGE_CART_SCAN_COUNT}条里发现的挂车短视频。",
                    f"- 挂车短视频样本数：{baseline_sample}",
                    "- 本报告只分析挂车短视频自己的互动底盘，不和主页普通短视频混算。",
                ]
                if is_cart_scope
                else [
            f"- {summary.get('cart_scan_label', f'主页前{HOMEPAGE_CART_SCAN_COUNT}条')}扫描作品数：{summary.get('cart_scan_count', 0)}",
                    f"- 发现挂车短视频数：{summary.get('cart_video_count', 0)}",
                    f"- 发现挂车短视频标题：{summary.get('cart_video_titles', '') or '无'}",
                    f"- 判定：{summary.get('cart_video_judgment', '')}",
                ]
            ),
            "",
            "## 5. 发布频率和具体发布时间",
            "",
            f"- 最近发布时间：{summary.get('latest_publish_time', '')}",
            f"- 最早样本发布时间：{summary.get('oldest_publish_time', '')}",
            f"- 近{baseline_sample}条{sample_label}时间跨度：{format_days(float(summary.get('publish_time_span_days', 0) or 0))}",
            f"- 相邻发布中位间隔：{format_days(float(summary.get('publish_interval_median_days', 0) or 0))}",
            f"- 相邻发布平均间隔：{format_days(float(summary.get('publish_interval_avg_days', 0) or 0))}",
            f"- 最大发布间隔：{format_days(float(summary.get('publish_interval_max_days', 0) or 0))}",
            f"- 发布节奏判断：{scoped_text(summary.get('publish_frequency_judgment', ''))}",
            f"- 发布时间段分布：{summary.get('publish_time_bucket_distribution', '')}",
            f"- 发布周几分布：{summary.get('publish_weekday_distribution', '')}",
            f"- 工作日/周末：工作日{summary.get('publish_weekday_count', 0)}条，周末{summary.get('publish_weekend_count', 0)}条。",
            f"- 点赞高位样本发布时间：{summary.get('high_sample_publish_timing', '')}",
            f"- 具体发布时间判断：{scoped_text(summary.get('publish_timing_judgment', ''))}",
            "",
            "## 6. 四项常态校验",
            "",
            f"- 点赞：中位数{summary.get('digg_median', 0)}，去极值平均{summary.get('digg_trimmed_mean', 0)}；高位样本{summary.get('digg_max', 0)}，低位样本{summary.get('digg_min', 0)}。",
            f"- 评论：中位数{summary.get('comment_median', 0)}，去极值平均{summary.get('comment_trimmed_mean', 0)}；高位样本{summary.get('comment_max', 0)}，低位样本{summary.get('comment_min', 0)}。",
            f"- 收藏：中位数{summary.get('collect_median', 0)}，去极值平均{summary.get('collect_trimmed_mean', 0)}；高位样本{summary.get('collect_max', 0)}，低位样本{summary.get('collect_min', 0)}。",
            f"- 分享：中位数{summary.get('share_median', 0)}，去极值平均{summary.get('share_trimmed_mean', 0)}；高位样本{summary.get('share_max', 0)}，低位样本{summary.get('share_min', 0)}。",
            f"- 去极值口径：每项分别排序，样本足够时去掉最高{summary.get('digg_trim_count', 0)}条、最低{summary.get('digg_trim_count', 0)}条后再算平均。",
            f"- {scoped_text(summary.get('digg_normal_judgment', ''))}",
            f"- {scoped_text(summary.get('comment_normal_judgment', ''))}",
            f"- {scoped_text(summary.get('collect_normal_judgment', ''))}",
            f"- {scoped_text(summary.get('share_normal_judgment', ''))}",
            "",
            "## 7. 四项波动判断",
            "",
            f"- 点赞：P25-P75为{summary.get('digg_p25', 0)}-{summary.get('digg_p75', 0)}，相对波动{summary.get('digg_relative_iqr', 0)}。",
            f"- 评论：P25-P75为{summary.get('comment_p25', 0)}-{summary.get('comment_p75', 0)}，相对波动{summary.get('comment_relative_iqr', 0)}。",
            f"- 收藏：P25-P75为{summary.get('collect_p25', 0)}-{summary.get('collect_p75', 0)}，相对波动{summary.get('collect_relative_iqr', 0)}。",
            f"- 分享：P25-P75为{summary.get('share_p25', 0)}-{summary.get('share_p75', 0)}，相对波动{summary.get('share_relative_iqr', 0)}。",
            f"- 变动最小：{summary.get('most_stable_dimension', '')}",
            f"- 变动最大：{summary.get('most_volatile_dimension', '')}",
            "",
            "## 8. 赞评藏转结构",
            "",
            f"- 近{baseline_sample}条中位结构：{summary.get('median_structure_raw', '')}",
            f"- 标准化结构：{summary.get('median_structure_normalized', '')}",
            f"- 结构判断：{summary.get('median_structure_note', '')}",
            f"- 结构稳定性：{summary.get('structure_consistency', '样本不足')}",
        ]
    )


def write_outputs(
    rows: list[dict[str, Any]],
    raw: dict[str, Any],
    target: str,
    out_dir: Path,
    *,
    target_baseline_count: int,
) -> dict[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    posts_csv_path = output_path(run_dir, "creator_posts.csv")
    cart_posts_csv_path = output_path(run_dir, "cart_posts.csv")
    summary_csv_path = output_path(run_dir, "interaction_summary.csv")
    cart_summary_csv_path = output_path(run_dir, "cart_interaction_summary.csv")
    json_path = run_dir / "raw.json"
    md_path = output_path(run_dir, "basic_profile_analysis.md")
    cart_md_path = output_path(run_dir, "cart_profile_analysis.md")
    normal_analysis_rows = baseline_rows(rows)
    cart_analysis_rows = homepage_cart_rows(rows)
    route = analysis_route(rows)
    raw["analysis_route"] = route
    summary = (
        build_summary(rows, target_baseline_count=target_baseline_count)
        if normal_analysis_rows
        else None
    )
    cart_summary = (
        build_summary(
            cart_analysis_rows,
            target_baseline_count=len(cart_analysis_rows),
            scope="cart",
        )
        if cart_analysis_rows
        else None
    )

    paths = {"json": str(json_path)}

    if normal_analysis_rows and summary:
        with posts_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            post_labels = [label for _, label in POSTS_CSV_COLUMNS]
            writer = csv.DictWriter(f, fieldnames=post_labels)
            writer.writeheader()
            writer.writerows(localized_row(row, POSTS_CSV_COLUMNS) for row in normal_analysis_rows)
        mirror_legacy(posts_csv_path, run_dir, "creator_posts.csv")
        with summary_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            summary_labels = [label for _, label in SUMMARY_CSV_COLUMNS]
            writer = csv.DictWriter(f, fieldnames=summary_labels)
            writer.writeheader()
            writer.writerow(localized_row(summary, SUMMARY_CSV_COLUMNS))
        mirror_legacy(summary_csv_path, run_dir, "interaction_summary.csv")
        raw["summary"] = summary
        md_path.write_text(build_analysis(rows, target, summary), encoding="utf-8")
        mirror_legacy(md_path, run_dir, "basic_profile_analysis.md")
        paths.update(
            {
                "posts_csv": str(posts_csv_path),
                "summary_csv": str(summary_csv_path),
                "analysis": str(md_path),
            }
        )

    if cart_analysis_rows:
        with cart_posts_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            post_labels = [label for _, label in POSTS_CSV_COLUMNS]
            writer = csv.DictWriter(f, fieldnames=post_labels)
            writer.writeheader()
            writer.writerows(localized_row(row, POSTS_CSV_COLUMNS) for row in cart_analysis_rows)
        mirror_legacy(cart_posts_csv_path, run_dir, "cart_posts.csv")
        paths["cart_posts_csv"] = str(cart_posts_csv_path)

    if cart_summary:
        with cart_summary_csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            summary_labels = [label for _, label in SUMMARY_CSV_COLUMNS]
            writer = csv.DictWriter(f, fieldnames=summary_labels)
            writer.writeheader()
            writer.writerow(localized_row(cart_summary, SUMMARY_CSV_COLUMNS))
        mirror_legacy(cart_summary_csv_path, run_dir, "cart_interaction_summary.csv")
        raw["cart_summary"] = cart_summary
        cart_md_path.write_text(
            build_analysis(cart_analysis_rows, target, cart_summary, scope="cart"), encoding="utf-8"
        )
        mirror_legacy(cart_md_path, run_dir, "cart_profile_analysis.md")
        paths["cart_summary_csv"] = str(cart_summary_csv_path)
        paths["cart_analysis"] = str(cart_md_path)

    json_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    target_url = normalize_target(args.target)
    resolved = resolve_url(target_url)
    aweme_id = extract_aweme_id(resolved)
    sec_uid = extract_sec_uid(resolved)

    raw: dict[str, Any] = {"input": args.target, "normalized_url": target_url, "resolved_url": resolved}
    items: list[dict[str, Any]]
    source: str
    if aweme_id:
        item = item_from_mobile_share(aweme_id)
        items = [item]
        source = "mobile_share_ssr"
    elif sec_uid:
        items = collect_profile_items_with_playwright(sec_uid, args.count)
        source = "web_profile_post_api"
    else:
        raise CollectError(f"target_type_not_supported:{resolved}")

    if not items:
        raise CollectError("no_items_collected")

    rows = [
        row_from_item(item, source=source, profile_position=index + 1 if sec_uid else None)
        for index, item in enumerate(items)
    ]
    annotate_pinned_rows(rows)
    if sec_uid:
        rows = trim_rows_to_baseline_target(rows, args.count)
    raw["items"] = items
    raw["row_count"] = len(rows)
    paths = write_outputs(rows, raw, args.target, out_dir, target_baseline_count=args.count)
    print(json.dumps({"status": "ok", "row_count": len(rows), "paths": paths}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CollectError, urllib.error.URLError, TimeoutError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
