"""從既有的 index.md 表格(或報告檔)建立 data/report_index.json,並重繪首頁。

首頁改為以 JSON 當資料來源後,需要一次性把舊的 Markdown 表格資料轉過去。
可重複執行:已有 JSON 時只重繪首頁,不動資料。

用法:python scripts/build_index_data.py
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.report import INDEX_DATA_REL, _load_index_rows, _render_index  # noqa: E402
from src.util import atomic_write_json  # noqa: E402

# | [2026-07-30](reports/2026-07-30.html) | 7 | 10 | [owner/repo](https://...)(★4) |
DATE_CELL_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
LINK_TEXT_RE = re.compile(r"\[([^\]]+)\]")
STARS_RE = re.compile(r"★(\d)")


def rows_from_index_md(index: Path) -> list[dict]:
    """逐欄解析表格列;整條 regex 容易被可選群組與全角括號絆倒,切欄位穩定得多。"""
    rows: list[dict] = []
    for line in index.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ["):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        date_m = DATE_CELL_RE.search(cells[0])
        if not date_m:
            continue
        name_m = LINK_TEXT_RE.search(cells[3])
        stars_m = STARS_RE.search(cells[3])
        rows.append({
            "date": date_m.group(1),
            "analyzed": int(cells[1]) if cells[1].isdigit() else 0,
            "cached": int(cells[2]) if cells[2].isdigit() else 0,
            "deferred": 0,
            "top_name": name_m.group(1) if name_m else "",
            "top_rating": int(stars_m.group(1)) if stars_m else 0,
        })
    return rows


def rows_from_reports(reports: Path) -> list[dict]:
    """退路:index.md 不可用時,由報告檔本身推導。"""
    block = re.compile(r"(?m)^### \[([^\]]+)\]\(https://github\.com/[^)]+\)\s*(★*)")
    full_count = re.compile(r"(?m)^- 本日完整分析 (\d+) 個$")
    light_count = re.compile(r"(?m)^- 輕量分析 (\d+) 個$")
    cached_count = re.compile(r"(?m)^- 持續上榜 (\d+) 個$")
    deferred_count = re.compile(r"(?m)^- 待分析 (\d+) 個$")
    rows = []
    for path in sorted(reports.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md")):
        text = path.read_text(encoding="utf-8")
        blocks = block.findall(text)
        best = max(blocks, key=lambda b: len(b[1]), default=None)
        full_m = full_count.search(text)
        light_m = light_count.search(text)
        cached_m = cached_count.search(text)
        deferred_m = deferred_count.search(text)
        rows.append({
            "date": path.stem,
            "analyzed": (
                (int(full_m.group(1)) if full_m else 0)
                + (int(light_m.group(1)) if light_m else 0)
            ),
            "cached": int(cached_m.group(1)) if cached_m else 0,
            "deferred": int(deferred_m.group(1)) if deferred_m else 0,
            "top_name": best[0] if best and best[1] else "",
            "top_rating": len(best[1]) if best else 0,
        })
    return rows


def main() -> int:
    log = logging.getLogger("build_index")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data_file = ROOT / INDEX_DATA_REL
    rows = _load_index_rows(data_file, log)
    source = "既有 JSON"

    if not rows:
        index = ROOT / "index.md"
        if index.exists():
            rows = rows_from_index_md(index)
            source = "index.md 表格"
        if not rows:
            rows = rows_from_reports(ROOT / "reports")
            source = "報告檔推導"

    if not rows:
        print("找不到任何報告資料", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    atomic_write_json(data_file, rows)
    (ROOT / "index.md").write_text(_render_index(rows, "reports"),
                                   encoding="utf-8", newline="\n")

    print(f"資料來源:{source}")
    for r in rows:
        print(f"  {r['date']}  分析 {r['analyzed']:>2}  持續 {r['cached']:>2}  "
              f"待分析 {r.get('deferred', 0):>2}  "
              f"之星 {r['top_name'] or '—'} ★{r['top_rating']}")
    print(f"\n已寫入 {INDEX_DATA_REL} 與 index.md(共 {len(rows)} 份)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
