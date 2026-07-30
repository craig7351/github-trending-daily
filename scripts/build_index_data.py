"""Build data/report_index.json and index.md from existing report files.

The parser supports both the current semantic HTML reports and legacy Markdown
reports so redesigning the site does not invalidate its history.

Usage: python scripts/build_index_data.py
"""
from __future__ import annotations

import html
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.report import INDEX_DATA_REL, _load_index_rows, _render_index  # noqa: E402
from src.util import atomic_write_json  # noqa: E402

DATE_CELL_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
LINK_TEXT_RE = re.compile(r"\[([^\]]+)\]")
STARS_RE = re.compile(r"★(\d)")

HTML_STATS_RE = re.compile(r'<section class="report-stats"[^>]*>')
DATA_ATTR_RE = re.compile(r'data-([a-z-]+)="(\d+)"')
HTML_REPO_RE = re.compile(
    r'<article class="[^"]*\brepo-article\b[^"]*"[^>]*'
    r'data-repo="([^"]+)"[^>]*data-rating="(\d+)"',
)
LEGACY_REPO_RE = re.compile(
    r"(?m)^### \[([^\]]+)\]\(https://github\.com/[^)]+\)\s*([★☆]*)"
)
LEGACY_FULL_RE = re.compile(r"(?m)^- 本日完整分析 (\d+) 個")
LEGACY_LIGHT_RE = re.compile(r"(?m)^- 輕量分析 (\d+) 個")
LEGACY_CACHED_RE = re.compile(r"(?m)^- 持續上榜 (\d+) 個")
LEGACY_DEFERRED_RE = re.compile(r"(?m)^- 待分析 (\d+) 個")


def rows_from_index_md(index: Path) -> list[dict]:
    """Read the old Markdown-table homepage format during migration."""
    rows: list[dict] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ["):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        date_match = DATE_CELL_RE.search(cells[0])
        if not date_match:
            continue
        name_match = LINK_TEXT_RE.search(cells[3])
        stars_match = STARS_RE.search(cells[3])
        rows.append({
            "date": date_match.group(1),
            "analyzed": int(cells[1]) if cells[1].isdigit() else 0,
            "cached": int(cells[2]) if cells[2].isdigit() else 0,
            "deferred": 0,
            "top_name": name_match.group(1) if name_match else "",
            "top_rating": int(stars_match.group(1)) if stars_match else 0,
        })
    return rows


def _row_from_html(path: Path, text: str) -> dict | None:
    stats_match = HTML_STATS_RE.search(text)
    if not stats_match:
        return None
    stats = {
        key: int(value)
        for key, value in DATA_ATTR_RE.findall(stats_match.group(0))
    }
    repos = [
        (html.unescape(name), int(rating))
        for name, rating in HTML_REPO_RE.findall(text)
    ]
    best = max(repos, key=lambda item: item[1], default=None)
    return {
        "date": path.stem,
        "analyzed": stats.get("analyzed", 0),
        "cached": stats.get("cached", 0),
        "deferred": stats.get("deferred", 0),
        "top_name": best[0] if best and best[1] else "",
        "top_rating": best[1] if best else 0,
    }


def _row_from_legacy_markdown(path: Path, text: str) -> dict:
    repos = LEGACY_REPO_RE.findall(text)
    best = max(repos, key=lambda item: item[1].count("★"), default=None)
    full_match = LEGACY_FULL_RE.search(text)
    light_match = LEGACY_LIGHT_RE.search(text)
    cached_match = LEGACY_CACHED_RE.search(text)
    deferred_match = LEGACY_DEFERRED_RE.search(text)
    return {
        "date": path.stem,
        "analyzed": (
            (int(full_match.group(1)) if full_match else 0)
            + (int(light_match.group(1)) if light_match else 0)
        ),
        "cached": int(cached_match.group(1)) if cached_match else 0,
        "deferred": int(deferred_match.group(1)) if deferred_match else 0,
        "top_name": best[0] if best and "★" in best[1] else "",
        "top_rating": best[1].count("★") if best else 0,
    }


def rows_from_reports(reports: Path) -> list[dict]:
    """Reconstruct homepage rows from current or historical report pages."""
    rows: list[dict] = []
    pattern = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"
    for path in sorted(reports.glob(pattern)):
        text = path.read_text(encoding="utf-8")
        rows.append(_row_from_html(path, text) or _row_from_legacy_markdown(path, text))
    return rows


def main() -> int:
    log = logging.getLogger("build_index")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data_file = ROOT / INDEX_DATA_REL
    rows = _load_index_rows(data_file, log)
    source = "現有 JSON"

    if not rows:
        index = ROOT / "index.md"
        if index.exists():
            rows = rows_from_index_md(index)
            source = "舊版 index.md"
        if not rows:
            rows = rows_from_reports(ROOT / "reports")
            source = "歷史報告"

    if not rows:
        print("找不到可建立索引的報告資料。", file=sys.stderr)
        return 1

    rows.sort(key=lambda row: str(row.get("date", "")), reverse=True)
    atomic_write_json(data_file, rows)
    (ROOT / "index.md").write_text(
        _render_index(rows, "reports"), encoding="utf-8", newline="\n"
    )

    print(f"資料來源：{source}")
    for row in rows:
        print(
            f"  {row['date']}  分析 {row['analyzed']:>2}  "
            f"持續 {row['cached']:>2}  待分析 {row.get('deferred', 0):>2}  "
            f"精選 {row['top_name'] or '—'} ★{row['top_rating']}"
        )
    print(f"\n已更新 {INDEX_DATA_REL} 與 index.md（共 {len(rows)} 份）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
