---
name: douyin-profile-video-collect
description: 输入抖音账号主页链接、账号分享文案、视频链接或 aweme_id 后，自动采集主页公开作品数据、识别置顶/挂车、生成赞评藏转基础盘，并在下载前向用户展示下载选项，待用户选择后再下载作品视频或图文媒体。Use when the user wants one independent skill for Douyin profile scraping, creator public interaction data collection, homepage work harvesting, account asset export, and user-confirmed video/media download from a Douyin profile URL.
---

# Douyin Profile Video Collect

## Overview

Use this skill when the task is: "给一个抖音主页链接，自动扒主页公开作品数据，下载前先让我选下载范围，再把作品媒体下载下来。"

This is an independent desktop copy that combines the useful parts of `douyin-creator-assets` and `video-download`. Do not assume it is registered in the original workspace; run it from this folder or reference scripts with absolute paths.

## Input

Accept any one of:

- Douyin account homepage URL
- Douyin account share text
- Douyin video URL or video share text
- `aweme_id`
- A `www.douyin.com/user/...` URL or a short `v.douyin.com/...` URL that resolves to a profile/video

If the user gives only "帮我扒一个主页" without a link, share text, or ID, ask for the missing Douyin input.

## Quick Start

Run the one-shot workflow first:

```bash
cd /Users/mayichao/Desktop/douyin-profile-video-collect
python3 scripts/collect_profile_and_download.py '抖音主页链接或分享文案'
```

Default behavior is `--download-mode ask`: collect first, then stop before download and show choices.

Specify target ordinary works count:

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --count 20
```

If the user has already chosen a download option, run it explicitly:

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --download-mode all-collected
```

Download only selected high/low interaction samples instead of all collected works:

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --download-mode samples --top-k 2
```

Only collect data, do not download media:

```bash
python3 scripts/collect_profile_and_download.py 'https://www.douyin.com/user/xxx' --download-mode none
```

Continue download selection from an existing run without collecting again:

```bash
python3 scripts/collect_profile_and_download.py --resume-run-dir '/path/to/timestamp_run_dir' --download-mode samples
```

## Workflow

1. Normalize the user's Douyin input. Pass the original share text or URL to the script; do not manually trim useful text unless needed.
2. Collect public works with `scripts/collect_creator_assets.py`.
3. Exclude pinned/likely pinned works from the ordinary homepage baseline.
4. Detect cart/product-anchor works using public interface fields such as `aweme_anchor_info`, `anchor_info`, `anchors`, `component_info_v2`, `commerce_config_data`, and product/shop fields.
5. Generate the public interaction baseline: likes, comments, collects, shares, total interaction, posting time, frequency, volatility, and normalized interaction structure.
6. Summarize available download choices from the collected rows.
7. Before downloading, present the user with the download options below and wait for an explicit choice. Do not choose for the user.
8. After the user chooses, continue with the matching `--download-mode` using the same run directory. Download selected media with `scripts/download_sample_videos.py`; video works become `.mp4`, image/text-image works become image folders plus JSON manifests.
9. For image/text-image works, reuse the collected `raw.json` item payload first when it already contains `images`/`image_infos`; do not force the mobile-share video route before trying image fields.

## Download Gate

Default `--download-mode ask` must be treated as a required checkpoint, not an error.

If the script can read from an interactive terminal, it asks directly:

1. Download all collected ordinary/cart works.
2. Download only high/low interaction samples.
3. Do not download media.

If the script runs in a non-interactive Codex command, it prints JSON with:

- `status: needs_download_choice`
- `run_dir`
- `options[].label`
- `options[].description`
- `options[].command`

When this happens, show the options to the user in chat and stop. Only run one of the returned `options[].command` values after the user chooses. Do not silently continue with `all-collected`.

## Download Modes

- `ask` is the default. It collects first, then requires user choice before media download.
- `all-collected` downloads every ordinary/cart work that entered the current collected sample.
- `samples` downloads metric-based high/low candidates selected by `scripts/select_video_samples.py`.
- `none` only produces data files and reports.

Use `--overwrite` when re-running a previous output directory and the user wants existing media replaced.

Use `--item-timeout <seconds>` if a single work hangs during media resolution or download. The default is 120 seconds per work; timed-out works are recorded as failed in the download result table so the batch can finish.

For image/text-image works, partial success still counts as image evidence: if at least one image file downloads, write the image manifest, mark the row as `image` success, and keep failed image indexes in the manifest `errors` list.

## Output

Default output root:

```text
/Users/mayichao/Desktop/douyin-profile-video-collect/outputs/douyin_profile_video_collect
```

Each run creates a timestamp directory. Important files:

- `raw.json`: original collected payload and route metadata.
- `01_主页普通作品明细.csv`: non-pinned, non-cart ordinary homepage works.
- `01_公开互动基础盘数据.csv`: ordinary works interaction summary.
- `01_公开互动基础盘.md`: ordinary works public interaction judgment.
- `01_挂车作品明细.csv`: cart/product-anchor works found in homepage scan.
- `01_挂车作品公开互动基础盘数据.csv`: cart works interaction summary.
- `01_挂车作品公开互动基础盘.md`: cart works public interaction judgment.
- `02B_媒体细看抽样清单数据.csv`: media download candidate list.
- `02B_候选媒体下载结果.csv`: download result table with local media paths and errors.
- `2b_<aweme_id>.mp4`: downloaded video files.
- `2b_<aweme_id>_images/`: downloaded image/text-image works.
- `2b_<aweme_id>.url.txt`: remote media URL captured during download.

## Boundaries

- Only use public-facing Douyin data returned by the web/mobile interfaces.
- Do not treat missing cart fields as proof that a work has no product anchor; say public interface did not find product/e-commerce fields.
- Do not infer ROI, conversion, cooperation value, or product fit from this skill alone.
- Do not treat playback count as a stable analysis field.
- Downloaded remote URLs may expire; local files are the durable artifact.

## Component Scripts

- `scripts/collect_profile_and_download.py`: one-shot orchestration entry.
- `scripts/collect_creator_assets.py`: public profile/video data collection and baseline report generation.
- `scripts/select_video_samples.py`: choose high/low interaction candidates for sample-mode download.
- `scripts/download_sample_videos.py`: download candidate videos or image/text-image media.
- `scripts/download_video.py`: fallback single-video downloader for one-off video links.
- `references/content_asset_model.md`: read only when doing content asset analysis beyond raw download.
- `references/prediction_model.md`: read when revising first-stage public interaction judgment logic.
