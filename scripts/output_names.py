"""Canonical Chinese output filenames with legacy English compatibility."""

from __future__ import annotations

import shutil
from pathlib import Path


CANONICAL_NAMES = {
    "creator_posts.csv": "01_主页普通作品明细.csv",
    "cart_posts.csv": "01_挂车作品明细.csv",
    "interaction_summary.csv": "01_公开互动基础盘数据.csv",
    "cart_interaction_summary.csv": "01_挂车作品公开互动基础盘数据.csv",
    "basic_profile_analysis.md": "01_公开互动基础盘.md",
    "cart_profile_analysis.md": "01_挂车作品公开互动基础盘.md",
    "content_asset_posts.csv": "02A_内容资产粗筛明细.csv",
    "content_asset_analysis.md": "02A_内容资产粗筛.md",
    "cart_content_asset_posts.csv": "02A_挂车作品内容资产粗筛明细.csv",
    "cart_content_asset_analysis.md": "02A_挂车作品内容资产粗筛.md",
    "video_sample_candidates.csv": "02B_媒体细看抽样清单数据.csv",
    "video_sample_candidates.md": "02B_媒体细看抽样清单.md",
    "downloaded_videos.csv": "02B_候选媒体下载结果.csv",
    "video_frame_grids.csv": "02B_视频抽帧结果.csv",
    "media_evidence_missing.csv": "02B_待补看媒体证据清单.csv",
    "media_evidence_missing.md": "02B_待补看媒体证据清单.md",
    "video_understanding_handoff.jsonl": "02B_媒体理解交接包.jsonl",
    "video_understanding_handoff.md": "02B_媒体理解交接包.md",
    "video_content_deep_dive.csv": "02B_媒体内容细看明细.csv",
    "video_content_deep_dive.md": "02B_媒体内容细看.md",
    "nutrition_transfer_prediction.csv": "02C_营养品议题转接预判明细.csv",
    "nutrition_transfer_prediction.md": "02C_营养品议题转接预判.md",
}


def output_path(run_dir: Path, legacy_name: str) -> Path:
    return run_dir / CANONICAL_NAMES.get(legacy_name, legacy_name)


def legacy_path(run_dir: Path, legacy_name: str) -> Path:
    return run_dir / legacy_name


def existing_output_path(run_dir: Path, legacy_name: str) -> Path:
    canonical = output_path(run_dir, legacy_name)
    if canonical.exists():
        return canonical
    return legacy_path(run_dir, legacy_name)


def mirror_legacy(canonical_path: Path, run_dir: Path, legacy_name: str) -> None:
    legacy = legacy_path(run_dir, legacy_name)
    if canonical_path == legacy:
        return
    if canonical_path.exists():
        shutil.copyfile(canonical_path, legacy)
