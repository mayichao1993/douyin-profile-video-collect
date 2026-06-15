#!/usr/bin/env python3
"""
Download a Douyin video from a share snippet, short link, canonical URL, or aweme_id.

Preferred strategy:
- Safari WebDriver reads the real player state from the canonical Douyin page.

Automatic fallback:
- If WebDriver is unavailable or the player never exposes a playable URL,
  open the page in Safari and call the Douyin detail API from page context.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "douyin_video_downloads"
AWEME_ID_RE = re.compile(r"\b(\d{10,25})\b")
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
URL_AWEME_PATTERNS = (
    re.compile(r"/video/(\d{10,25})"),
    re.compile(r"[?&]modal_id=(\d{10,25})"),
    re.compile(r"[?&]aweme_id=(\d{10,25})"),
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
FORMAT_DIRS = {
    "mp4": "mp4",
    "json": "json",
    "txt": "txt",
}
VIDEO_ONLY_URL_MARKERS = (
    "media-video",
    "/play/dash/",
    "video_dash",
)
AUDIO_URL_MARKERS = (
    "media-audio",
    "audio_mp4",
    "audio_m4a",
    "mime_type=audio",
    "mp4a",
)


class DouyinVideoDownloadError(RuntimeError):
    """Raised when the downloader cannot resolve or fetch a Douyin video."""


@dataclass
class WebDriverSession:
    port: int
    process: subprocess.Popen[bytes]
    session_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(self, method: str, path: str, data: Any | None = None, *, timeout: int = 120) -> Any:
        payload = None
        headers = {}
        if data is not None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise DouyinVideoDownloadError(f"webdriver_http_error:{exc.code}: {body}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise DouyinVideoDownloadError(f"webdriver_invalid_json: {body[:500]}") from exc

    def wait_until_ready(self, *, timeout_seconds: float = 45.0) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with suppress(Exception):
                status = self.request("GET", "/status", timeout=10)
                ready = status.get("value", {}).get("ready")
                if ready is True:
                    return
            time.sleep(0.2)
        raise DouyinVideoDownloadError("safaridriver_not_ready")

    def create_session(self) -> None:
        resp = self.request(
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": {"browserName": "safari"}}},
            timeout=120,
        )
        self.session_id = resp["value"]["sessionId"]

    def close_session(self) -> None:
        if not self.session_id:
            return
        with suppress(Exception):
            self.request("DELETE", f"/session/{self.session_id}", timeout=30)
        self.session_id = None

    def shutdown(self) -> None:
        self.close_session()
        if self.process.poll() is None:
            self.process.terminate()
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)
        if self.process.poll() is None:
            self.process.kill()

    def navigate(self, url: str) -> None:
        self._ensure_session()
        self.request("POST", f"/session/{self.session_id}/url", {"url": url}, timeout=180)

    def execute_sync(self, script: str, args: list[Any] | None = None, *, timeout: int = 180) -> Any:
        self._ensure_session()
        resp = self.request(
            "POST",
            f"/session/{self.session_id}/execute/sync",
            {"script": script, "args": args or []},
            timeout=timeout,
        )
        return resp.get("value")

    def _ensure_session(self) -> None:
        if not self.session_id:
            raise DouyinVideoDownloadError("webdriver_session_not_created")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a Douyin video from share text, short link, canonical URL, or aweme_id."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "target",
        nargs="?",
        help="Douyin share text, short URL, canonical URL, or aweme_id digits.",
    )
    source_group.add_argument(
        "--batch-file",
        default=None,
        help="TXT file containing multiple targets. Use blank lines to separate multi-line share snippets.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Output directory. Default: outputs/douyin_video_downloads",
    )
    parser.add_argument(
        "--filename",
        default=None,
        help="Optional output filename without directory. Defaults to <title>_<aweme_id>.mp4",
    )
    parser.add_argument(
        "--load-wait",
        type=float,
        default=8.0,
        help="Initial seconds to wait after opening the page. Default: 8",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=25.0,
        help="Maximum seconds to wait for the video URL to appear. Default: 25",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Polling interval while waiting for the player to initialize. Default: 1",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Resolve metadata and print JSON without downloading the file.",
    )
    parser.add_argument(
        "--batch-summary",
        default=None,
        help="Optional JSON path for batch summary output. Only valid with --batch-file.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop immediately when one batch item fails.",
    )
    parser.add_argument(
        "--allow-headed-fallback",
        action="store_true",
        help="Allow Safari/WebDriver fallback strategies when headless strategies fail.",
    )
    args = parser.parse_args()
    if args.batch_file and args.filename:
        parser.error("--filename cannot be used with --batch-file")
    if args.batch_summary and not args.batch_file:
        parser.error("--batch-summary can only be used with --batch-file")
    if args.fail_fast and not args.batch_file:
        parser.error("--fail-fast can only be used with --batch-file")
    return args


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_safaridriver() -> WebDriverSession:
    port = free_port()
    process = subprocess.Popen(
        ["safaridriver", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    session = WebDriverSession(port=port, process=process)
    try:
        session.wait_until_ready()
        session.create_session()
        return session
    except Exception:
        session.shutdown()
        raise


def trim_url(candidate: str) -> str:
    return candidate.rstrip('，。！？、,.!?"\'）)]}>')


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return trim_url(match.group(0))


def extract_aweme_id(text: str) -> str | None:
    for pattern in URL_AWEME_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    standalone = AWEME_ID_RE.search(text)
    if standalone:
        return standalone.group(1)
    return None


def normalize_target_to_url(target: str) -> str:
    target = target.strip()
    url = extract_first_url(target)
    if url:
        return url
    aweme_id = extract_aweme_id(target)
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}"
    raise DouyinVideoDownloadError("target_must_contain_a_douyin_url_or_aweme_id")


def open_url(url: str, *, timeout: int = 60, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def resolve_url(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    with open_url(url, timeout=60, headers=headers) as resp:
        return resp.geturl()


def canonical_video_url(url: str) -> str:
    aweme_id = extract_aweme_id(url)
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}"
    return url


def sanitize_filename(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = text.strip(" .")
    return text[:180] or "douyin_video"


def run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
) -> str:
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DouyinVideoDownloadError(f"command_not_found:{command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DouyinVideoDownloadError(f"command_timeout:{command[0]}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit_code={completed.returncode}"
        raise DouyinVideoDownloadError(f"command_failed:{command[0]}: {detail}")
    return completed.stdout.strip()


def run_applescript(script: str, *, timeout: int = 120) -> str:
    return run_command(["osascript", "-"], input_text=script, timeout=timeout)


def safari_open_url(page_url: str) -> None:
    script = f"""
tell application "Safari"
    activate
    if (count of windows) = 0 then
        make new document with properties {{URL:{json.dumps(page_url)}}}
    else
        set URL of current tab of front window to {json.dumps(page_url)}
    end if
end tell
"""
    run_applescript(script, timeout=60)


def safari_run_javascript(script_body: str, *, timeout: int = 120) -> str:
    script = f"""
tell application "Safari"
    if (count of windows) = 0 then error "safari_has_no_window"
    return do JavaScript {json.dumps(script_body)} in current tab of front window
end tell
"""
    return run_applescript(script, timeout=timeout)


def coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d-]", "", value)
        if digits:
            with suppress(ValueError):
                return int(digits)
    return 0


def normalize_duration_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        with suppress(ValueError):
            value = float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1000:
            return round(number / 1000.0, 3)
        return round(number, 3)
    return None


def extract_download_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    raw_candidates = snapshot.get("download_candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                candidates.append(item)
    if candidates:
        return candidates

    cur_definition = snapshot.get("cur_definition") or {}
    urls = [url for url in cur_definition.get("urls") or [] if isinstance(url, str)]
    for url in urls:
        candidates.append(
            {
                "url": url,
                "size": cur_definition.get("size"),
                "bit_rate": cur_definition.get("bit_rate"),
                "quality_name": cur_definition.get("quality_name"),
                "quality_type": cur_definition.get("quality_type"),
                "source": "cur_definition",
            }
        )
    return candidates


def fetch_player_snapshot(session: WebDriverSession) -> dict[str, Any]:
    script = r"""
const player = window.player || null;
const video = document.querySelector("video");
const cur = player && player.curDefinition ? player.curDefinition : null;
const normalizeUrlList = (items) =>
  Array.isArray(items)
    ? items.map((item) => {
        if (!item) return null;
        if (typeof item === "string") return item;
        if (typeof item.src === "string") return item.src;
        return null;
      }).filter(Boolean)
    : [];
const titleText = (document.title || "").replace(/\s*-\s*抖音\s*$/, "").trim();
return {
  href: location.href,
  title: titleText,
  ready_state: document.readyState,
  aweme_id: (location.pathname.match(/\/video\/(\d{10,25})/) || [null, null])[1],
  video_present: !!video,
  video_blob_src: video ? (video.currentSrc || video.src || null) : null,
  duration: player ? (player._duration || null) : null,
  cur_definition: cur ? {
    vid: cur.vid || null,
    uri: cur.uri || null,
    size: cur.size || null,
    bit_rate: cur.bit_rate || null,
    quality_type: cur.quality_type || null,
    quality_name: cur.quality_name || null,
    urls: normalizeUrlList(cur.url),
  } : null,
  resource_urls: performance
    .getEntriesByType("resource")
    .map((entry) => entry.name)
    .filter((name) => /douyinvod|media-video|media-audio|mime_type=(video|audio)|play\/dash/.test(name))
    .slice(-20),
  audio_resource_urls: performance
    .getEntriesByType("resource")
    .map((entry) => entry.name)
    .filter((name) => /media-audio|mime_type=audio|audio_mp4|audio_m4a|mp4a/.test(name))
    .slice(-10),
};
"""
    value = session.execute_sync(script, timeout=120)
    if not isinstance(value, dict):
        raise DouyinVideoDownloadError(f"invalid_player_snapshot: {value!r}")
    return value


def wait_for_video_info(
    session: WebDriverSession,
    page_url: str,
    *,
    load_wait: float,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    session.navigate(page_url)
    time.sleep(max(0.0, load_wait))
    deadline = time.time() + max(1.0, wait_timeout)
    last_snapshot: dict[str, Any] | None = None
    while time.time() < deadline:
        snapshot = fetch_player_snapshot(session)
        last_snapshot = snapshot
        cur_definition = snapshot.get("cur_definition") or {}
        urls = [url for url in cur_definition.get("urls") or [] if isinstance(url, str)]
        if urls:
            return snapshot
        time.sleep(max(0.2, poll_interval))
    raise DouyinVideoDownloadError(
        "video_url_not_found_in_player_state:"
        f" {json.dumps(last_snapshot or {}, ensure_ascii=False)[:1000]}"
    )


def resolve_snapshot_via_webdriver(args: argparse.Namespace, page_url: str) -> dict[str, Any]:
    session: WebDriverSession | None = None
    try:
        session = start_safaridriver()
        snapshot = wait_for_video_info(
            session,
            page_url,
            load_wait=args.load_wait,
            wait_timeout=args.wait_timeout,
            poll_interval=args.poll_interval,
        )
    finally:
        if session:
            session.shutdown()
    snapshot["resolve_method"] = "webdriver_player"
    return snapshot


def fetch_player_snapshot_via_safari() -> dict[str, Any]:
    script = r"""
JSON.stringify((() => {
  const player = window.player || null;
  const video = document.querySelector("video");
  const cur = player && player.curDefinition ? player.curDefinition : null;
  const normalizeUrlList = (items) =>
    Array.isArray(items)
      ? items.flatMap((item) => {
          if (!item) return null;
          if (typeof item === "string") return item;
          if (typeof item.src === "string") return item.src;
          return ["main_url", "backup_url_1", "backup_url", "fallback_url"]
            .map((key) => typeof item[key] === "string" ? item[key] : null)
            .filter(Boolean);
        }).filter(Boolean)
      : [];
  const definitionUrls = [
    ...normalizeUrlList(cur ? cur.url : null),
    ...(cur && typeof cur.main_url === "string" ? [cur.main_url] : []),
    ...(cur && typeof cur.backup_url_1 === "string" ? [cur.backup_url_1] : []),
    ...(cur && typeof cur.backup_url === "string" ? [cur.backup_url] : []),
    ...(cur && typeof cur.fallback_url === "string" ? [cur.fallback_url] : []),
  ].filter((url, index, list) => url && list.indexOf(url) === index);
  let titleText = (document.title || "").trim();
  const suffixIndex = titleText.lastIndexOf(" - ");
  if (suffixIndex >= 0) {
    titleText = titleText.slice(0, suffixIndex).trim();
  }
  const resources = performance.getEntriesByType("resource").map((entry) => entry.name);
  const pathParts = location.pathname.split("/");
  const videoIndex = pathParts.indexOf("video");
  return {
    href: location.href,
    title: titleText,
    ready_state: document.readyState,
    aweme_id: videoIndex >= 0 ? (pathParts[videoIndex + 1] || null) : null,
    video_present: !!video,
    video_blob_src: video ? (video.currentSrc || video.src || null) : null,
    duration: player ? (player._duration || null) : (video ? video.duration : null),
    cur_definition: cur ? {
      vid: cur.vid || null,
      uri: cur.uri || null,
      size: cur.size || null,
      bit_rate: cur.bit_rate || cur.bitrate || cur.realBitrate || null,
      quality_type: cur.quality_type || cur.qualityType || null,
      quality_name: cur.quality_name || cur.qualityName || cur.gearName || null,
      urls: definitionUrls,
    } : null,
    resource_urls: resources
      .filter((name) => name.includes("douyinvod") || name.includes("media-video") || name.includes("media-audio") || name.includes("mime_type=video") || name.includes("mime_type=audio") || name.includes("play/dash"))
      .slice(-40),
    audio_resource_urls: resources
      .filter((name) => name.includes("media-audio") || name.includes("mime_type=audio") || name.includes("audio_mp4") || name.includes("audio_m4a") || name.includes("mp4a"))
      .slice(-20),
  };
})())
"""
    raw = safari_run_javascript(script, timeout=60)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DouyinVideoDownloadError(f"invalid_safari_player_snapshot: {raw[:500]}") from exc
    if not isinstance(value, dict):
        raise DouyinVideoDownloadError(f"invalid_safari_player_snapshot: {value!r}")
    return value


def resolve_snapshot_via_safari_player(args: argparse.Namespace, page_url: str) -> dict[str, Any]:
    safari_open_url(page_url)
    time.sleep(max(0.0, min(args.load_wait, 5.0)))
    deadline = time.time() + max(5.0, args.wait_timeout + args.load_wait)
    last_snapshot: dict[str, Any] | None = None
    while time.time() < deadline:
        snapshot = fetch_player_snapshot_via_safari()
        last_snapshot = snapshot
        cur_definition = snapshot.get("cur_definition") or {}
        urls = [url for url in cur_definition.get("urls") or [] if isinstance(url, str)]
        audio_urls = extract_audio_candidates(snapshot)
        if urls and (audio_urls or any(candidate_likely_has_audio(item) is not False for item in extract_download_candidates(snapshot))):
            snapshot["resolve_method"] = "safari_player"
            return snapshot
        time.sleep(max(0.3, args.poll_interval))
    raise DouyinVideoDownloadError(
        "video_url_not_found_in_safari_player_state:"
        f" {json.dumps(last_snapshot or {}, ensure_ascii=False)[:1000]}"
    )


def wait_for_safari_page_ready(page_url: str, *, wait_timeout: float, poll_interval: float) -> None:
    deadline = time.time() + max(5.0, wait_timeout)
    last_state = ""
    while time.time() < deadline:
        raw = safari_run_javascript(
            """JSON.stringify({
  href: location.href,
  readyState: document.readyState,
  title: document.title || ""
})""",
            timeout=30,
        )
        last_state = raw
        with suppress(json.JSONDecodeError):
            state = json.loads(raw) if raw else {}
            href = str(state.get("href") or "")
            ready_state = str(state.get("readyState") or "")
            if ready_state == "complete" and (not page_url or href.startswith("https://www.douyin.com/")):
                return
        time.sleep(max(0.3, poll_interval))
    raise DouyinVideoDownloadError(f"safari_page_not_ready: {last_state[:300]}")


def fetch_aweme_detail_via_safari_page_api(
    aweme_id: str,
    *,
    wait_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    bootstrap_script = f"""
(() => {{
  const awemeId = {json.dumps(aweme_id)};
  const storeKey = "__codexDouyinAwemeDetail";
  window[storeKey] = {{
    status: "pending",
    awemeId,
    startedAt: Date.now()
  }};
  const apiUrl = `/aweme/v1/web/aweme/detail/?aweme_id=${{encodeURIComponent(awemeId)}}&request_source=600&origin_type=video_page`;
  fetch(apiUrl, {{ credentials: "include" }})
    .then(async (resp) => {{
      const body = await resp.text();
      window[storeKey] = {{
        status: "done",
        ok: resp.ok,
        statusCode: resp.status,
        body
      }};
    }})
    .catch((error) => {{
      window[storeKey] = {{
        status: "error",
        message: String(error)
      }};
    }});
  return "started";
}})();
"""
    safari_run_javascript(bootstrap_script, timeout=60)
    deadline = time.time() + max(5.0, wait_timeout)
    last_payload = ""
    while time.time() < deadline:
        raw = safari_run_javascript(
            'JSON.stringify(window.__codexDouyinAwemeDetail || null)',
            timeout=30,
        )
        last_payload = raw
        if not raw:
            time.sleep(max(0.3, poll_interval))
            continue
        with suppress(json.JSONDecodeError):
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                time.sleep(max(0.3, poll_interval))
                continue
            status = payload.get("status")
            if status == "pending":
                time.sleep(max(0.3, poll_interval))
                continue
            if status == "error":
                raise DouyinVideoDownloadError(f"safari_page_api_error: {payload.get('message')}")
            if status != "done":
                time.sleep(max(0.3, poll_interval))
                continue
            if not payload.get("ok"):
                raise DouyinVideoDownloadError(
                    f"safari_page_api_http_error:{payload.get('statusCode')}"
                )
            body = payload.get("body")
            if not isinstance(body, str) or not body.strip():
                raise DouyinVideoDownloadError("safari_page_api_empty_response")
            try:
                detail_payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise DouyinVideoDownloadError(
                    f"safari_page_api_invalid_json: {body[:300]}"
                ) from exc
            aweme_detail = detail_payload.get("aweme_detail")
            if not isinstance(aweme_detail, dict):
                raise DouyinVideoDownloadError(
                    f"safari_page_api_missing_aweme_detail: {body[:300]}"
                )
            return aweme_detail
        time.sleep(max(0.3, poll_interval))
    raise DouyinVideoDownloadError(f"safari_page_api_timeout: {last_payload[:300]}")


def build_snapshot_from_aweme_detail(
    aweme_detail: dict[str, Any],
    *,
    page_url: str,
    method: str,
) -> dict[str, Any]:
    video = aweme_detail.get("video") or {}
    music = aweme_detail.get("music") or {}
    candidates: list[dict[str, Any]] = []
    audio_candidates: list[dict[str, Any]] = []
    seen_video_urls: set[str] = set()
    seen_audio_urls: set[str] = set()

    def add_candidate(urls: Any, *, source: str, payload: dict[str, Any] | None = None) -> None:
        if not isinstance(urls, list):
            return
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            if url in seen_video_urls:
                continue
            seen_video_urls.add(url)
            candidate: dict[str, Any] = {"url": url, "source": source}
            if isinstance(payload, dict):
                candidate["bit_rate"] = payload.get("bit_rate")
                candidate["quality_name"] = payload.get("gear_name") or payload.get("quality_name")
                candidate["quality_type"] = payload.get("quality_type")
                candidate["size"] = (
                    payload.get("data_size")
                    or payload.get("size")
                    or ((payload.get("play_addr") or {}).get("data_size") if isinstance(payload.get("play_addr"), dict) else None)
                )
            candidates.append(candidate)

    def add_audio_candidate(urls: Any, *, source: str, payload: dict[str, Any] | None = None) -> None:
        if not isinstance(urls, list):
            return
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            if url in seen_audio_urls:
                continue
            seen_audio_urls.add(url)
            candidate: dict[str, Any] = {"url": url, "source": source}
            if isinstance(payload, dict):
                candidate["size"] = payload.get("data_size") or payload.get("size")
                candidate["quality_name"] = payload.get("format") or payload.get("quality_name")
            audio_candidates.append(candidate)

    for field_name in ("download_addr", "play_addr", "play_addr_h264", "play_addr_bytevc1"):
        play_addr = video.get(field_name) or {}
        add_candidate(
            play_addr.get("url_list"),
            source=field_name,
            payload=play_addr if isinstance(play_addr, dict) else None,
        )

    bit_rates = video.get("bit_rate") or []
    if isinstance(bit_rates, list):
        for item in bit_rates:
            if not isinstance(item, dict):
                continue
            play_addr = item.get("play_addr") or {}
            add_candidate(play_addr.get("url_list"), source="bit_rate", payload=item)

    music_play_url = music.get("play_url") or {}
    add_audio_candidate(
        music_play_url.get("url_list"),
        source="music_play_url",
        payload=music_play_url if isinstance(music_play_url, dict) else None,
    )

    if not candidates:
        raise DouyinVideoDownloadError("no_downloadable_video_url_found_in_aweme_detail")

    preferred = choose_download_candidate({"download_candidates": candidates})
    title = (
        aweme_detail.get("desc")
        or aweme_detail.get("preview_title")
        or aweme_detail.get("aweme_id")
        or "douyin_video"
    )
    return {
        "href": page_url,
        "title": title,
        "ready_state": "complete",
        "aweme_id": aweme_detail.get("aweme_id") or extract_aweme_id(page_url),
        "video_present": True,
        "video_blob_src": None,
        "duration": normalize_duration_seconds(video.get("duration")),
        "cur_definition": {
            "vid": ((video.get("play_addr") or {}).get("uri")) if isinstance(video.get("play_addr"), dict) else None,
            "uri": ((video.get("play_addr") or {}).get("uri")) if isinstance(video.get("play_addr"), dict) else None,
            "size": preferred.get("size"),
            "bit_rate": preferred.get("bit_rate"),
            "quality_type": preferred.get("quality_type"),
            "quality_name": preferred.get("quality_name"),
            "urls": [item["url"] for item in candidates if isinstance(item.get("url"), str)],
          },
        "download_candidates": candidates,
        "audio_candidates": audio_candidates,
        "resource_urls": [],
        "resolve_method": method,
    }


def resolve_snapshot_via_safari_page_api(args: argparse.Namespace, page_url: str) -> dict[str, Any]:
    aweme_id = extract_aweme_id(page_url)
    if not aweme_id:
        raise DouyinVideoDownloadError("missing_aweme_id_for_safari_page_api")
    safari_open_url(page_url)
    time.sleep(max(0.0, min(args.load_wait, 5.0)))
    wait_for_safari_page_ready(
        page_url,
        wait_timeout=args.wait_timeout,
        poll_interval=args.poll_interval,
    )
    aweme_detail = fetch_aweme_detail_via_safari_page_api(
        aweme_id,
        wait_timeout=args.wait_timeout,
        poll_interval=args.poll_interval,
    )
    return build_snapshot_from_aweme_detail(
        aweme_detail,
        page_url=page_url,
        method="safari_page_api",
    )


def resolve_snapshot_via_headless_page_api(args: argparse.Namespace, page_url: str) -> dict[str, Any]:
    aweme_id = extract_aweme_id(page_url)
    if not aweme_id:
        raise DouyinVideoDownloadError("missing_aweme_id_for_headless_page_api")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise DouyinVideoDownloadError(f"playwright_not_available: {exc}") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(int(max(1000.0, min(args.load_wait * 1000.0, 5000.0))))
            payload = page.evaluate(
                """
async (currentAwemeId) => {
  const apiUrl = `/aweme/v1/web/aweme/detail/?aweme_id=${encodeURIComponent(currentAwemeId)}&request_source=600&origin_type=video_page`;
  const response = await fetch(apiUrl, { credentials: "include" });
  const body = await response.text();
  return {
    ok: response.ok,
    status: response.status,
    body,
  };
}
""",
                aweme_id,
            )
        finally:
            browser.close()

    if not isinstance(payload, dict):
        raise DouyinVideoDownloadError(f"headless_page_api_invalid_payload: {payload!r}")
    if not payload.get("ok"):
        raise DouyinVideoDownloadError(
            f"headless_page_api_http_error:{payload.get('status')}"
        )
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise DouyinVideoDownloadError("headless_page_api_empty_response")
    try:
        detail_payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DouyinVideoDownloadError(
            f"headless_page_api_invalid_json: {body[:300]}"
        ) from exc
    aweme_detail = detail_payload.get("aweme_detail")
    if not isinstance(aweme_detail, dict):
        raise DouyinVideoDownloadError(
            f"headless_page_api_missing_aweme_detail: {body[:300]}"
        )
    return build_snapshot_from_aweme_detail(
        aweme_detail,
        page_url=page_url,
        method="headless_page_api",
    )


def yt_dlp_command() -> list[str] | None:
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    with suppress(Exception):
        import yt_dlp  # type: ignore  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    return None


def resolve_snapshot_via_yt_dlp(page_url: str) -> dict[str, Any]:
    command = yt_dlp_command()
    if not command:
        raise DouyinVideoDownloadError("yt_dlp_not_installed")
    raw = run_command(
        [*command, "--dump-single-json", "--no-playlist", "--no-warnings", page_url],
        timeout=180,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DouyinVideoDownloadError(f"yt_dlp_invalid_json: {raw[:300]}") from exc

    candidates: list[dict[str, Any]] = []
    audio_candidates: list[dict[str, Any]] = []
    formats = payload.get("formats")
    if isinstance(formats, list):
        for item in formats:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            candidate = {
                "url": url,
                "size": item.get("filesize") or item.get("filesize_approx"),
                "bit_rate": item.get("tbr"),
                "quality_name": item.get("format_note") or item.get("format"),
                "quality_type": item.get("format_id"),
                "vcodec": item.get("vcodec"),
                "acodec": item.get("acodec"),
                "source": "yt_dlp_format",
            }
            if str(item.get("vcodec") or "").lower() == "none":
                audio_candidates.append(candidate)
            else:
                candidates.append(candidate)

    direct_url = payload.get("url")
    if isinstance(direct_url, str) and direct_url:
        candidates.append(
            {
                "url": direct_url,
                "size": payload.get("filesize") or payload.get("filesize_approx"),
                "bit_rate": payload.get("tbr"),
                "quality_name": payload.get("format_note") or payload.get("format"),
                "quality_type": payload.get("format_id"),
                "vcodec": payload.get("vcodec"),
                "acodec": payload.get("acodec"),
                "source": "yt_dlp_direct",
            }
        )

    if not candidates:
        raise DouyinVideoDownloadError("yt_dlp_has_no_downloadable_url")

    preferred = choose_download_candidate({"download_candidates": candidates})
    return {
        "href": page_url,
        "title": payload.get("title") or payload.get("fulltitle") or payload.get("id") or "douyin_video",
        "ready_state": "complete",
        "aweme_id": payload.get("id") or extract_aweme_id(page_url),
        "video_present": True,
        "video_blob_src": None,
        "duration": normalize_duration_seconds(payload.get("duration")),
        "cur_definition": {
            "vid": None,
            "uri": None,
            "size": preferred.get("size"),
            "bit_rate": preferred.get("bit_rate"),
            "quality_type": preferred.get("quality_type"),
            "quality_name": preferred.get("quality_name"),
            "urls": [item["url"] for item in candidates if isinstance(item.get("url"), str)],
        },
        "download_candidates": candidates,
        "audio_candidates": audio_candidates,
        "resource_urls": [],
        "resolve_method": "yt_dlp",
    }


def resolve_video_snapshot(
    args: argparse.Namespace,
    page_url: str,
    *,
    skip_methods: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    attempts: list[dict[str, str]] = []
    skip_methods = skip_methods or set()
    strategies: list[tuple[str, Any]] = [
        ("headless_page_api", lambda: resolve_snapshot_via_headless_page_api(args, page_url)),
        ("yt_dlp", lambda: resolve_snapshot_via_yt_dlp(page_url)),
    ]
    if args.allow_headed_fallback:
        strategies.extend(
            [
                ("safari_player", lambda: resolve_snapshot_via_safari_player(args, page_url)),
                ("webdriver_player", lambda: resolve_snapshot_via_webdriver(args, page_url)),
                ("safari_page_api", lambda: resolve_snapshot_via_safari_page_api(args, page_url)),
            ]
        )
    for strategy_name, resolver in strategies:
        if strategy_name in skip_methods:
            attempts.append({"method": strategy_name, "status": "skipped"})
            continue
        try:
            snapshot = resolver()
            if not snapshot_has_audio_recovery_plan(snapshot):
                raise DouyinVideoDownloadError("strategy_returned_only_known_video_only_urls")
            attempts.append({"method": strategy_name, "status": "success"})
            snapshot["resolve_method"] = snapshot.get("resolve_method") or strategy_name
            return snapshot, attempts
        except Exception as exc:
            attempts.append({"method": strategy_name, "status": "failed", "error": str(exc)})
    attempt_summary = "; ".join(
        f"{item['method']}={item.get('error', item['status'])}" for item in attempts
    )
    raise DouyinVideoDownloadError(f"all_download_strategies_failed: {attempt_summary}")


def is_probably_video_only_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in VIDEO_ONLY_URL_MARKERS)


def is_probably_audio_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in AUDIO_URL_MARKERS)


def candidate_likely_has_audio(item: dict[str, Any]) -> bool | None:
    acodec = str(item.get("acodec") or "").lower()
    if acodec and acodec != "none":
        return True
    if acodec == "none":
        return False
    url = str(item.get("url") or "")
    if is_probably_video_only_url(url):
        return False
    if is_probably_audio_url(url):
        return False
    source = str(item.get("source") or "")
    if source in {"download_addr", "yt_dlp_direct"}:
        return True
    return None


def extract_audio_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str, *, source: str, payload: dict[str, Any] | None = None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        item: dict[str, Any] = {"url": url, "source": source}
        if payload:
            item.update({k: v for k, v in payload.items() if k != "url"})
        candidates.append(item)

    raw_candidates = snapshot.get("audio_candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str):
                add(url, source=str(item.get("source") or "audio_candidate"), payload=item)

    for key in ("audio_resource_urls", "resource_urls"):
        urls = snapshot.get(key)
        if not isinstance(urls, list):
            continue
        for url in urls:
            if isinstance(url, str) and is_probably_audio_url(url):
                add(url, source=key)

    return candidates


def ranked_download_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = extract_download_candidates(snapshot)
    if not candidates:
        raise DouyinVideoDownloadError("no_downloadable_video_url_found")

    def score(item: dict[str, Any]) -> tuple[int, int, int, int, int]:
        url = str(item.get("url") or "")
        host = urllib.parse.urlparse(url).netloc.lower()
        is_mp4 = int("mime_type=video_mp4" in url or "media-video" in url or host.endswith("douyinvod.com"))
        is_dash_endpoint = int("/play/dash/" in url)
        likely_has_audio = candidate_likely_has_audio(item)
        return (
            int(likely_has_audio is True),
            is_mp4,
            coerce_int(item.get("bit_rate")),
            coerce_int(item.get("size")),
            -is_dash_endpoint,
        )

    return sorted(candidates, key=score, reverse=True)


def snapshot_has_audio_recovery_plan(snapshot: dict[str, Any]) -> bool:
    if extract_audio_candidates(snapshot):
        return True
    return any(candidate_likely_has_audio(candidate) is not False for candidate in extract_download_candidates(snapshot))


def choose_download_candidate(snapshot: dict[str, Any]) -> dict[str, Any]:
    return ranked_download_candidates(snapshot)[0]


def choose_download_url(snapshot: dict[str, Any]) -> str:
    return str(choose_download_candidate(snapshot).get("url"))


def format_bytes(size: int | None) -> str | None:
    if size is None:
        return None
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return None


def ensure_format_dir(out_dir: Path, format_name: str) -> Path:
    folder_name = FORMAT_DIRS[format_name]
    path = out_dir / folder_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_output_paths(
    snapshot: dict[str, Any],
    out_dir: Path,
    explicit_filename: str | None,
) -> tuple[Path, Path, Path, Path]:
    aweme_id = snapshot.get("aweme_id") or "unknown"
    title = sanitize_filename(snapshot.get("title") or "douyin_video")
    filename = explicit_filename or f"{title}_{aweme_id}.mp4"
    if not filename.lower().endswith(".mp4"):
        filename = f"{filename}.mp4"
    base_name = sanitize_filename(Path(filename).stem)
    video_path = ensure_format_dir(out_dir, "mp4") / f"{base_name}.mp4"
    meta_path = ensure_format_dir(out_dir, "json") / f"{base_name}.json"
    gemini_handoff_path = ensure_format_dir(out_dir, "json") / f"{base_name}.gemini.json"
    url_text_path = ensure_format_dir(out_dir, "txt") / f"{base_name}.url.txt"
    return video_path, meta_path, gemini_handoff_path, url_text_path


def download_file(url: str, destination: Path) -> tuple[int | None, str | None]:
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        total_header = resp.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        content_type = resp.headers.get("Content-Type")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_suffix(destination.suffix + ".part")
        downloaded = 0
        last_report = time.time()
        with tmp_path.open("wb") as handle:
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_report >= 0.8:
                    progress = format_bytes(downloaded) or str(downloaded)
                    total_text = format_bytes(total) or "unknown"
                    print(f"下载中: {progress} / {total_text}", file=sys.stderr)
                    last_report = now
        os.replace(tmp_path, destination)
        return total or downloaded, content_type


def inspect_mp4_tracks(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    handlers: list[str] = []
    offset = 0
    while True:
        index = data.find(b"hdlr", offset)
        if index < 0:
            break
        handler = data[index + 12 : index + 16]
        if len(handler) == 4:
            with suppress(UnicodeDecodeError):
                handlers.append(handler.decode("ascii"))
        offset = index + 4
    return {
        "track_handlers": sorted(set(handlers)),
        "has_video_track": "vide" in handlers,
        "has_audio_track": "soun" in handlers,
    }


def find_executable(names: list[str]) -> str | None:
    common_dirs = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        if name == "ffmpeg":
            with suppress(Exception):
                import imageio_ffmpeg  # type: ignore

                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                if ffmpeg and Path(ffmpeg).exists() and os.access(ffmpeg, os.X_OK):
                    return str(ffmpeg)
        for directory in common_dirs:
            candidate = Path(directory) / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def merge_with_ffmpeg(ffmpeg: str, video_path: Path, audio_path: Path, output_path: Path) -> None:
    run_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(output_path),
        ],
        timeout=600,
    )


def merge_video_and_audio(video_path: Path, audio_path: Path, output_path: Path) -> str:
    ffmpeg = find_executable(["ffmpeg"])
    if ffmpeg:
        merge_with_ffmpeg(ffmpeg, video_path, audio_path, output_path)
        return "ffmpeg"
    raise DouyinVideoDownloadError("audio_merge_requires_ffmpeg")


def cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        with suppress(FileNotFoundError):
            path.unlink()


def download_video_with_audio_recovery(
    snapshot: dict[str, Any],
    initial_candidate: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    candidates = ranked_download_candidates(snapshot)
    candidates = [initial_candidate] + [
        candidate for candidate in candidates if candidate.get("url") != initial_candidate.get("url")
    ]
    audio_candidates = extract_audio_candidates(snapshot)
    errors: list[str] = []

    for candidate in candidates:
        video_url = str(candidate.get("url") or "")
        if not video_url:
            continue

        working_video_path = destination.with_name(destination.name + ".video.mp4")
        working_audio_path = destination.with_name(destination.name + ".audio.m4a")
        merged_path = destination.with_name(destination.name + ".merged.mp4")
        cleanup_paths([working_video_path, working_audio_path, merged_path])

        try:
            actual_size, content_type = download_file(video_url, working_video_path)
            tracks = inspect_mp4_tracks(working_video_path)
            if tracks.get("has_audio_track"):
                os.replace(working_video_path, destination)
                return {
                    "actual_size": actual_size,
                    "content_type": content_type,
                    "media_tracks": tracks,
                    "has_audio_track": True,
                    "remote_video_url_has_audio": True,
                    "selected_download_candidate": candidate,
                    "download_url": video_url,
                    "audio_recovery": "source_had_audio",
                }

            for audio_candidate in audio_candidates:
                audio_url = str(audio_candidate.get("url") or "")
                if not audio_url:
                    continue
                try:
                    download_file(audio_url, working_audio_path)
                    merge_method = merge_video_and_audio(working_video_path, working_audio_path, merged_path)
                    merged_tracks = inspect_mp4_tracks(merged_path)
                    if not merged_tracks.get("has_audio_track"):
                        raise DouyinVideoDownloadError("merged_file_still_has_no_audio_track")
                    os.replace(merged_path, destination)
                    return {
                        "actual_size": destination.stat().st_size,
                        "content_type": "video/mp4",
                        "media_tracks": merged_tracks,
                        "has_audio_track": True,
                        "remote_video_url_has_audio": False,
                        "selected_download_candidate": candidate,
                        "download_url": video_url,
                        "audio_download_url": audio_url,
                        "selected_audio_candidate": audio_candidate,
                        "audio_merge_method": merge_method,
                        "audio_recovery": "merged_separate_audio",
                    }
                except Exception as exc:
                    errors.append(f"audio_merge_failed:{audio_candidate.get('source')}:{exc}")
                    cleanup_paths([working_audio_path, merged_path])

            if not audio_candidates:
                if candidate_likely_has_audio(candidate) is False:
                    errors.append(f"video_candidate_has_no_audio_and_no_audio_candidate:{candidate.get('source')}")
                    continue
                os.replace(working_video_path, destination)
                return {
                    "actual_size": actual_size,
                    "content_type": content_type,
                    "media_tracks": tracks,
                    "has_audio_track": False,
                    "remote_video_url_has_audio": False,
                    "selected_download_candidate": candidate,
                    "download_url": video_url,
                    "audio_recovery": "no_audio_candidate_found",
                    "warning": "downloaded_file_has_no_audio_track_and_no_audio_candidate_was_found",
                }

            errors.append(f"video_candidate_has_no_audio:{candidate.get('source')}")
        except Exception as exc:
            errors.append(f"video_download_failed:{candidate.get('source')}:{exc}")
        finally:
            cleanup_paths([working_video_path, working_audio_path, merged_path])

    raise DouyinVideoDownloadError(
        "downloaded_video_has_no_audio_track: " + "; ".join(errors[-8:])
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_batch_targets(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"\n\s*\n", normalized):
        chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", normalized) if chunk.strip()]
        return [" ".join(line.strip() for line in chunk.split("\n") if line.strip()) for chunk in chunks]
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def build_batch_summary_path(out_dir: Path, batch_file: Path, explicit_path: str | None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
    return ensure_format_dir(out_dir, "json") / f"{sanitize_filename(batch_file.stem)}_batch_summary_{timestamp}.json"


def parse_signed_url_expiry(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        raw = (query.get("dy_q") or [None])[0]
        if not raw:
            return None
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).astimezone().isoformat()
    except Exception:
        return None


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def build_gemini_handoff(metadata: dict[str, Any]) -> dict[str, Any]:
    remote_url = metadata.get("download_url")
    local_path = metadata.get("video_path")
    prompt_placeholder = "这个视频讲了什么？"
    gemini_script = str((PROJECT_ROOT / "视频理解" / "gemini_video_chat.mjs").resolve())
    remote_has_audio = metadata.get("remote_video_url_has_audio")
    preferred_input = "remote_video_url" if remote_url else "local_video_path"
    if remote_has_audio is False and local_path:
        preferred_input = "local_video_path"

    notes = [
        "远程 URL 是从抖音页面或可用解析链路里拿到的签名地址，通常可直接喂给支持公网 URL 的视频理解接口。",
        "这个远程 URL 往往是临时的，过期后请重新跑一次下载脚本拿新链接。",
        "如果服务端不接受公网 URL，也可以退回本地路径模式。",
    ]
    if remote_has_audio is False and metadata.get("has_audio_track"):
        notes.append("本次远程视频 URL 是视频-only，脚本已把单独音频合进本地 mp4；需要声音时优先使用 local_video_path。")

    return {
        "title": metadata.get("title"),
        "aweme_id": metadata.get("aweme_id"),
        "resolve_method": metadata.get("resolve_method"),
        "remote_video_url": remote_url,
        "remote_video_url_has_audio": remote_has_audio,
        "remote_video_url_expires_at": parse_signed_url_expiry(remote_url),
        "local_video_path": local_path,
        "local_video_has_audio": metadata.get("has_audio_track"),
        "preferred_for_gemini": preferred_input,
        "notes": notes,
        "gemini_examples": {
            "remote_url": (
                f"node {shell_quote(gemini_script)} "
                f"--video {shell_quote(str(remote_url or 'https://example.com/video.mp4'))} "
                f"--prompt {shell_quote(prompt_placeholder)}"
            ),
            "local_file": (
                f"node {shell_quote(gemini_script)} "
                f"--video {shell_quote(str(local_path or '/absolute/path/video.mp4'))} "
                f"--prompt {shell_quote(prompt_placeholder)}"
            ),
        },
    }


def process_single_target(
    args: argparse.Namespace,
    out_dir: Path,
    raw_target: str,
    *,
    print_json: bool = True,
) -> dict[str, Any]:
    normalized_input_url = normalize_target_to_url(raw_target)
    resolved_url = resolve_url(normalized_input_url)
    page_url = canonical_video_url(resolved_url)
    skipped_methods: set[str] = set()
    combined_resolve_attempts: list[dict[str, str]] = []
    download_failures: list[dict[str, str]] = []
    metadata: dict[str, Any] | None = None

    while True:
        try:
            snapshot, resolve_attempts = resolve_video_snapshot(
                args,
                page_url,
                skip_methods=skipped_methods,
            )
        except Exception as exc:
            failure_summary = "; ".join(
                f"{item['method']}={item.get('error', item['status'])}"
                for item in combined_resolve_attempts
            )
            download_summary = "; ".join(
                f"{item['method']}={item['error']}" for item in download_failures
            )
            detail = "; ".join(part for part in (failure_summary, download_summary, str(exc)) if part)
            raise DouyinVideoDownloadError(f"all_resolve_or_download_attempts_failed: {detail}") from exc

        combined_resolve_attempts.extend(resolve_attempts)
        resolve_method = str(snapshot.get("resolve_method") or "")
        if resolve_method:
            skipped_methods.add(resolve_method)

        download_candidate = choose_download_candidate(snapshot)
        download_url = str(download_candidate.get("url"))
        video_path, meta_path, gemini_handoff_path, url_text_path = build_output_paths(snapshot, out_dir, args.filename)
        metadata = {
            "input": raw_target,
            "normalized_input_url": normalized_input_url,
            "resolved_url": resolved_url,
            "page_url": page_url,
            "download_url": download_url,
            "aweme_id": snapshot.get("aweme_id"),
            "title": snapshot.get("title"),
            "duration_seconds": snapshot.get("duration"),
            "declared_size_bytes": download_candidate.get("size"),
            "declared_size_human": format_bytes(download_candidate.get("size")),
            "resolve_method": snapshot.get("resolve_method"),
            "resolve_attempts": combined_resolve_attempts,
            "download_attempt_failures": download_failures,
            "selected_download_candidate": download_candidate,
            "available_audio_candidate_count": len(extract_audio_candidates(snapshot)),
            "video_path": str(video_path),
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(),
        }

        if args.skip_download:
            break

        try:
            download_result = download_video_with_audio_recovery(snapshot, download_candidate, video_path)
        except DouyinVideoDownloadError as exc:
            download_failures.append({"method": resolve_method or "unknown", "error": str(exc)})
            if resolve_method and len(skipped_methods) < 5:
                continue
            raise

        download_candidate = download_result["selected_download_candidate"]
        download_url = str(download_result["download_url"])
        metadata["download_url"] = download_url
        metadata["selected_download_candidate"] = download_candidate
        metadata["downloaded_size_bytes"] = download_result.get("actual_size")
        metadata["downloaded_size_human"] = format_bytes(download_result.get("actual_size"))
        metadata["content_type"] = download_result.get("content_type")
        metadata["media_tracks"] = download_result.get("media_tracks")
        metadata["has_audio_track"] = download_result.get("has_audio_track")
        metadata["remote_video_url_has_audio"] = download_result.get("remote_video_url_has_audio")
        metadata["audio_recovery"] = download_result.get("audio_recovery")
        if download_result.get("audio_download_url"):
            metadata["audio_download_url"] = download_result.get("audio_download_url")
        if download_result.get("selected_audio_candidate"):
            metadata["selected_audio_candidate"] = download_result.get("selected_audio_candidate")
        if download_result.get("audio_merge_method"):
            metadata["audio_merge_method"] = download_result.get("audio_merge_method")
        if download_result.get("warning"):
            metadata["warning"] = download_result.get("warning")
        metadata["meta_path"] = str(meta_path)
        break

    if metadata is None:
        raise DouyinVideoDownloadError("internal_error:metadata_not_created")

    gemini_handoff = build_gemini_handoff(metadata)
    metadata["gemini_handoff_path"] = str(gemini_handoff_path)
    metadata["url_text_path"] = str(url_text_path)
    metadata["remote_video_url_expires_at"] = gemini_handoff.get("remote_video_url_expires_at")

    if metadata.get("download_url"):
        write_text(url_text_path, f"{metadata['download_url']}\n")
    write_json(gemini_handoff_path, gemini_handoff)
    if not args.skip_download:
        write_json(meta_path, metadata)

    if print_json:
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata


def process_batch(args: argparse.Namespace, out_dir: Path) -> int:
    batch_file = Path(args.batch_file).expanduser().resolve()
    targets = load_batch_targets(batch_file)
    if not targets:
        raise DouyinVideoDownloadError(f"batch_file_has_no_valid_targets: {batch_file}")

    summary_path = build_batch_summary_path(out_dir, batch_file, args.batch_summary)
    results: list[dict[str, Any]] = []
    success_count = 0
    failure_count = 0

    for index, raw_target in enumerate(targets, start=1):
        print(f"[batch {index}/{len(targets)}] 开始处理", file=sys.stderr)
        try:
            item = process_single_target(args, out_dir, raw_target, print_json=False)
            results.append(
                {
                    "index": index,
                    "status": "success",
                    "input": raw_target,
                    "aweme_id": item.get("aweme_id"),
                    "title": item.get("title"),
                    "video_path": item.get("video_path"),
                    "download_url": item.get("download_url"),
                    "url_text_path": item.get("url_text_path"),
                    "gemini_handoff_path": item.get("gemini_handoff_path"),
                    "meta_path": item.get("meta_path"),
                    "remote_video_url_expires_at": item.get("remote_video_url_expires_at"),
                    "has_audio_track": item.get("has_audio_track"),
                    "audio_recovery": item.get("audio_recovery"),
                }
            )
            success_count += 1
        except Exception as exc:
            failure_count += 1
            results.append(
                {
                    "index": index,
                    "status": "failed",
                    "input": raw_target,
                    "error": str(exc),
                }
            )
            print(f"[batch {index}/{len(targets)}] 失败: {exc}", file=sys.stderr)
            if args.fail_fast:
                break

    summary = {
        "batch_file": str(batch_file),
        "total": len(results),
        "success_count": success_count,
        "failure_count": failure_count,
        "skip_download": bool(args.skip_download),
        "out_dir": str(out_dir),
        "summary_path": str(summary_path),
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "results": results,
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failure_count == 0 else 1


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir).expanduser().resolve()
    if args.batch_file:
        return process_batch(args, out_dir)
    process_single_target(args, out_dir, args.target, print_json=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DouyinVideoDownloadError as exc:
        print(f"下载失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
