#!/usr/bin/env python3
"""Download candidate media selected for the 2B deep dive.

Video works are saved as mp4. Image/text-image works are saved as image files
plus a small manifest so downstream agents can inspect them without assuming
every Douyin work is a video.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import http.client
import json
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from collect_creator_assets import MOBILE_USER_AGENT, CollectError, item_from_mobile_share
from output_names import existing_output_path, mirror_legacy, output_path


OUTPUT_COLUMNS = [
    "作品ID",
    "作品标题",
    "抽样锚点",
    "媒体类型",
    "下载状态",
    "视频文件",
    "图片文件",
    "图片清单文件",
    "视频URL文件",
    "错误信息",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download 2B candidate media from video_sample_candidates.csv."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    parser.add_argument(
        "--source",
        default="video_sample_candidates.csv",
        help="Candidate CSV filename inside run_dir.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing downloaded media files.",
    )
    parser.add_argument(
        "--item-timeout",
        type=int,
        default=120,
        help="Maximum seconds to spend on one media item. Use 0 to disable.",
    )
    return parser.parse_args()


@contextlib.contextmanager
def time_limit(seconds: int):
    if seconds <= 0:
        yield
        return

    def raise_timeout(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"item_timeout_after_{seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_collected_items(run_dir: Path) -> dict[str, dict[str, Any]]:
    raw_path = run_dir / "raw.json"
    if not raw_path.exists():
        return {}
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = payload.get("items")
    if not isinstance(items, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("aweme_id") or item.get("aweme_id_str") or "").strip()
        if aweme_id:
            indexed[aweme_id] = item
    return indexed


def get_nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def unique_append(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def candidate_video_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    video = item.get("video") or {}

    address_candidates = [
        video.get("play_addr"),
        video.get("play_addr_h264"),
        video.get("download_addr"),
        video.get("play_addr_bytevc1"),
    ]
    for addr in address_candidates:
        if isinstance(addr, dict):
            for url in addr.get("url_list") or []:
                if isinstance(url, str):
                    unique_append(urls, url)

    for bit_rate in video.get("bit_rate") or []:
        if isinstance(bit_rate, dict):
            play_addr = bit_rate.get("play_addr") or {}
            for url in play_addr.get("url_list") or []:
                if isinstance(url, str):
                    unique_append(urls, url)

    # Some mobile-share payloads use nested detail fields.
    fallback = get_nested(item, "video_info", "download_addr", "url_list")
    if isinstance(fallback, list):
        for url in fallback:
            if isinstance(url, str):
                unique_append(urls, url)
    return urls


def urls_from_image_obj(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        unique_append(urls, value)
    elif isinstance(value, list):
        for item in value:
            for url in urls_from_image_obj(item):
                unique_append(urls, url)
    elif isinstance(value, dict):
        for key in [
            "url_list",
            "download_url_list",
            "large_url_list",
            "origin_url_list",
            "display_url_list",
        ]:
            child = value.get(key)
            if isinstance(child, list):
                for url in child:
                    if isinstance(url, str):
                        unique_append(urls, url)
        for key in [
            "url",
            "uri",
            "download_url",
            "origin_url",
            "display_url",
            "large_url",
        ]:
            child = value.get(key)
            if isinstance(child, str) and child.startswith(("http://", "https://")):
                unique_append(urls, child)
        for key in [
            "image",
            "image_info",
            "display_image",
            "download_image",
            "origin_image",
            "large_image",
        ]:
            if key in value:
                for url in urls_from_image_obj(value.get(key)):
                    unique_append(urls, url)
    return urls


def best_url_from_image(value: Any) -> str:
    if isinstance(value, dict):
        for key in ["url_list", "download_url_list", "large_url_list", "origin_url_list"]:
            child = value.get(key)
            if isinstance(child, list):
                for url in child:
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        return url
        for key in ["url", "download_url", "origin_url", "display_url", "large_url"]:
            child = value.get(key)
            if isinstance(child, str) and child.startswith(("http://", "https://")):
                return child
    urls = urls_from_image_obj(value)
    return urls[0] if urls else ""


def candidate_image_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    image_containers = [
        item.get("images"),
        item.get("image_infos"),
        item.get("img_list"),
        item.get("images_info"),
        get_nested(item, "image_post_info", "images"),
        get_nested(item, "image_post_info", "image_infos"),
        get_nested(item, "video", "images"),
    ]
    for container in image_containers:
        if isinstance(container, list):
            for image_obj in container:
                unique_append(urls, best_url_from_image(image_obj))
        else:
            unique_append(urls, best_url_from_image(container))
    return urls


def download_url(
    url: str,
    target_path: Path,
    *,
    socket_timeout: int = 60,
    max_seconds: int = 0,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": MOBILE_USER_AGENT})
    temp_path = target_path.with_suffix(target_path.suffix + ".part")
    bytes_written = 0
    incomplete_error = ""
    deadline = time.monotonic() + max_seconds if max_seconds > 0 else 0
    try:
        with urllib.request.urlopen(request, timeout=socket_timeout) as response, temp_path.open("wb") as f:
            while True:
                if deadline and time.monotonic() > deadline:
                    raise TimeoutError(f"url_download_timeout_after_{max_seconds}s")
                try:
                    chunk = response.read(1024 * 512)
                except http.client.IncompleteRead as exc:
                    chunk = exc.partial
                    incomplete_error = str(exc)
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
                if not chunk or incomplete_error:
                    break
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    if bytes_written <= 0:
        temp_path.unlink(missing_ok=True)
        raise CollectError("empty_media_response")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(target_path)


def extension_from_url(url: str, default: str = ".jpg") -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        if path.endswith(ext):
            return ext
    return default


def existing_image_paths(image_dir: Path) -> list[Path]:
    if not image_dir.exists():
        return []
    return sorted(
        path
        for path in image_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )


def is_image_work(item: dict[str, Any], image_urls: list[str]) -> bool:
    video = item.get("video") or {}
    duration = int(video.get("duration") or 0)
    aweme_type = int(item.get("aweme_type") or 0)
    has_image_container = any(
        item.get(key)
        for key in ["images", "image_infos", "img_list", "images_info", "image_post_info"]
    )
    return bool(image_urls) and (aweme_type in {2, 68} or duration == 0 or has_image_container)


def choose_item_for_download(aweme_id: str, collected_items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    collected_item = collected_items.get(aweme_id)
    if collected_item:
        collected_image_urls = candidate_image_urls(collected_item)
        if is_image_work(collected_item, collected_image_urls):
            return collected_item
    try:
        return item_from_mobile_share(aweme_id)
    except Exception:
        if collected_item:
            return collected_item
        raise


def save_image_work(
    *,
    image_urls: list[str],
    image_dir: Path,
    image_manifest_path: Path,
    url_path: Path,
    raw_path: Path,
    item: dict[str, Any],
    aweme_id: str,
    title: str,
) -> list[Path]:
    image_dir.mkdir(parents=True, exist_ok=True)
    image_paths: list[Path] = []
    downloaded_urls: list[str] = []
    errors: list[str] = []
    for index, url in enumerate(image_urls, start=1):
        image_path = image_dir / f"{index:02d}{extension_from_url(url)}"
        if image_path.exists() and image_path.stat().st_size > 0:
            image_paths.append(image_path)
            downloaded_urls.append(url)
            continue
        try:
            download_url(url, image_path, socket_timeout=10, max_seconds=20)
            image_paths.append(image_path)
            downloaded_urls.append(url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{index}:{exc}")
    if not image_paths:
        raise CollectError("image_download_failed:" + "；".join(errors))
    url_path.write_text("\n".join(downloaded_urls) + "\n", encoding="utf-8")
    raw_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    image_manifest_path.write_text(
        json.dumps(
            {
                "aweme_id": aweme_id,
                "title": title,
                "media_type": "image",
                "image_files": [str(path) for path in image_paths],
                "image_urls": downloaded_urls,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return image_paths


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    source_path = existing_output_path(run_dir, args.source)
    rows = read_csv(source_path)
    collected_items = load_collected_items(run_dir)

    result_rows: list[dict[str, str]] = []
    for row in rows:
        if row.get("建议下载", "是") not in {"是", "yes", "Y", "y", "1", "true", "True"}:
            continue
        aweme_id = str(row.get("作品ID") or "").strip()
        title = str(row.get("作品标题") or "").strip()
        anchors = str(row.get("抽样锚点") or "").strip()
        if not aweme_id:
            result_rows.append(
                {
                    "作品ID": "",
                    "作品标题": title,
                    "抽样锚点": anchors,
                    "媒体类型": "",
                    "下载状态": "失败",
                    "视频文件": "",
                    "图片文件": "",
                    "图片清单文件": "",
                    "视频URL文件": "",
                    "错误信息": "missing_aweme_id",
                }
            )
            continue

        filename = f"2b_{aweme_id}.mp4"
        video_path = run_dir / filename
        image_dir = run_dir / f"2b_{aweme_id}_images"
        image_manifest_path = run_dir / f"2b_{aweme_id}_images.json"
        url_path = run_dir / f"2b_{aweme_id}.url.txt"
        raw_path = run_dir / f"2b_{aweme_id}.raw.json"
        if video_path.exists() and not args.overwrite:
            result_rows.append(
                {
                    "作品ID": aweme_id,
                    "作品标题": title,
                    "抽样锚点": anchors,
                    "媒体类型": "video",
                    "下载状态": "已存在",
                    "视频文件": str(video_path),
                    "图片文件": "",
                    "图片清单文件": "",
                    "视频URL文件": str(url_path) if url_path.exists() else "",
                    "错误信息": "",
                }
            )
            continue
        existing_images = existing_image_paths(image_dir)
        if existing_images and image_manifest_path.exists() and not args.overwrite:
            result_rows.append(
                {
                    "作品ID": aweme_id,
                    "作品标题": title,
                    "抽样锚点": anchors,
                    "媒体类型": "image",
                    "下载状态": "已存在",
                    "视频文件": "",
                    "图片文件": "；".join(str(path) for path in existing_images),
                    "图片清单文件": str(image_manifest_path),
                    "视频URL文件": str(url_path) if url_path.exists() else "",
                    "错误信息": "",
                }
            )
            continue

        try:
            with time_limit(args.item_timeout):
                item = choose_item_for_download(aweme_id, collected_items)
                video_urls = candidate_video_urls(item)
                image_urls = candidate_image_urls(item)
                last_error = ""
                media_type = ""
                image_paths: list[Path] = []

                if is_image_work(item, image_urls):
                    media_type = "image"
                    image_paths = save_image_work(
                        image_urls=image_urls,
                        image_dir=image_dir,
                        image_manifest_path=image_manifest_path,
                        url_path=url_path,
                        raw_path=raw_path,
                        item=item,
                        aweme_id=aweme_id,
                        title=title,
                    )
                elif video_urls:
                    media_type = "video"
                    for url in video_urls:
                        try:
                            download_url(url, video_path)
                            url_path.write_text(url + "\n", encoding="utf-8")
                            raw_path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                            last_error = ""
                            break
                        except Exception as exc:  # noqa: BLE001
                            last_error = str(exc)
                    if last_error:
                        if image_urls:
                            media_type = "image"
                            image_paths = save_image_work(
                                image_urls=image_urls,
                                image_dir=image_dir,
                                image_manifest_path=image_manifest_path,
                                url_path=url_path,
                                raw_path=raw_path,
                                item=item,
                                aweme_id=aweme_id,
                                title=title,
                            )
                        else:
                            raise CollectError(last_error)
                elif image_urls:
                    media_type = "image"
                    image_paths = save_image_work(
                        image_urls=image_urls,
                        image_dir=image_dir,
                        image_manifest_path=image_manifest_path,
                        url_path=url_path,
                        raw_path=raw_path,
                        item=item,
                        aweme_id=aweme_id,
                        title=title,
                    )
                else:
                    raise CollectError("media_url_not_found")
            status = "成功"
            error = ""
        except (CollectError, urllib.error.URLError, TimeoutError, OSError) as exc:
            status = "失败"
            error = str(exc)
            media_type = ""
            image_paths = []

        result_rows.append(
            {
                "作品ID": aweme_id,
                "作品标题": title,
                "抽样锚点": anchors,
                "媒体类型": media_type,
                "下载状态": status,
                "视频文件": str(video_path) if video_path.exists() else "",
                "图片文件": "；".join(str(path) for path in image_paths),
                "图片清单文件": str(image_manifest_path) if image_manifest_path.exists() else "",
                "视频URL文件": str(url_path) if url_path.exists() else "",
                "错误信息": error,
            }
        )

    out_path = output_path(run_dir, "downloaded_videos.csv")
    write_csv(out_path, result_rows)
    mirror_legacy(out_path, run_dir, "downloaded_videos.csv")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
