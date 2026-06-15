#!/usr/bin/env python3
"""Render first-step profile analysis documents from an existing run directory."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from collect_creator_assets import (
    SUMMARY_CSV_COLUMNS,
    build_analysis,
    humanize_cell,
)
from output_names import existing_output_path, legacy_path, mirror_legacy, output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render basic_profile_analysis.md and cart_profile_analysis.md from raw.json or summary CSV files."
    )
    parser.add_argument("run_dir", help="A outputs/douyin_creator_assets/<timestamp> directory.")
    return parser.parse_args()


def reverse_humanize(value: Any) -> Any:
    mapping = {
        "是": "yes",
        "否": "no",
        "未知": "unknown",
        "疑似": "likely",
        "低": "low",
        "中": "medium",
        "高": "high",
        "移动分享页公开数据": "mobile_share_ssr",
        "主页作品公开接口": "web_profile_post_api",
        "接口标记置顶": "is_top",
        "主页前排作品早于后续更新，疑似置顶": "chronology_top_card_older_than_later_newer_work",
    }
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def read_summary_csv(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    reverse_columns = {label: key for key, label in SUMMARY_CSV_COLUMNS}
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    localized = rows[0]
    return {
        reverse_columns.get(label, label): reverse_humanize(value)
        for label, value in localized.items()
    }


def load_raw(run_dir: Path) -> dict[str, Any]:
    raw_path = run_dir / "raw.json"
    if not raw_path.exists():
        return {}
    return json.loads(raw_path.read_text(encoding="utf-8"))


def ensure_summary(run_dir: Path, raw: dict[str, Any]) -> dict[str, Any]:
    summary = raw.get("summary")
    if isinstance(summary, dict):
        return summary
    csv_summary = read_summary_csv(existing_output_path(run_dir, "interaction_summary.csv"))
    if csv_summary:
        return csv_summary
    raise FileNotFoundError("Need raw.json with summary or interaction_summary.csv.")


def ensure_cart_summary(run_dir: Path, raw: dict[str, Any]) -> dict[str, Any] | None:
    cart_summary = raw.get("cart_summary")
    if isinstance(cart_summary, dict):
        return cart_summary
    return read_summary_csv(existing_output_path(run_dir, "cart_interaction_summary.csv"))


def remove_generated(run_dir: Path, legacy_name: str) -> None:
    for path in {output_path(run_dir, legacy_name), legacy_path(run_dir, legacy_name)}:
        if path.exists():
            path.unlink()


def should_render_normal(summary: dict[str, Any] | None, route: dict[str, Any]) -> bool:
    if not summary:
        return False
    if route.get("mode") == "cart_only":
        return False
    try:
        return int(float(str(summary.get("baseline_sample_count") or 0))) > 0
    except Exception:
        return True


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    raw = load_raw(run_dir)
    target = str(raw.get("input") or raw.get("normalized_url") or run_dir)
    route = raw.get("analysis_route") if isinstance(raw.get("analysis_route"), dict) else {}
    summary = None
    try:
        summary = ensure_summary(run_dir, raw)
    except FileNotFoundError:
        summary = None
    cart_summary = ensure_cart_summary(run_dir, raw)

    paths: dict[str, str] = {}
    if should_render_normal(summary, route):
        basic_path = output_path(run_dir, "basic_profile_analysis.md")
        basic_path.write_text(build_analysis([], target, summary), encoding="utf-8")
        mirror_legacy(basic_path, run_dir, "basic_profile_analysis.md")
        paths["basic_profile_analysis"] = str(basic_path)
    else:
        remove_generated(run_dir, "basic_profile_analysis.md")

    if cart_summary:
        cart_path = output_path(run_dir, "cart_profile_analysis.md")
        cart_path.write_text(build_analysis([], target, cart_summary, scope="cart"), encoding="utf-8")
        mirror_legacy(cart_path, run_dir, "cart_profile_analysis.md")
        paths["cart_profile_analysis"] = str(cart_path)
    else:
        remove_generated(run_dir, "cart_profile_analysis.md")

    print(json.dumps({"status": "ok", "paths": paths}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
