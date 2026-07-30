"""將既有報告以現行版面重新產生(只換呈現,不改內容)。

用途:報告版面改版後,讓歷史報告也套用新設計。

資料來源:
- 分區、順序、語言與 star 數:原報告檔本身(最忠實於當天的實際產出)
- 分析內文:data/seen_repos.json 的快取(不重新呼叫 AI,零額度成本)
- 連續/累計上榜天數:去重檔推算(比舊報告的數字更正確 —— 舊版把中斷
  也算成連續)

刻意不做的事:
- 不加「事後補跑」說明:這些報告當天就準時產生了,那樣的敘述是錯的
- 不從快照補進原報告沒有的 repo:快照是同日多次執行的聯集,不等於
  當天單次掃描的名單,補進去等於憑空造內容

用法:
    python scripts/rerender_reports.py            # 全部重繪
    python scripts/rerender_reports.py 2026-07-25 # 只重繪指定日期
    python scripts/rerender_reports.py --check     # 只檢查可否重繪,不寫檔
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import CachedEntry, RepoResult, TrendingRepo  # noqa: E402
from src.report import render_report  # noqa: E402
from src.store import SeenStore  # noqa: E402

# ### [owner/repo](https://github.com/owner/repo) ★★★★☆
BLOCK_RE = re.compile(r"^### \[([^\]]+)\]\((https://github\.com/[^)]+)\)[ \t]*(★*)", re.M)
# 🗣 TypeScript | ⭐ 4,288(今日 +201)| 分類:app | 上榜第 1 天 |(輕量分析)
META_RE = re.compile(r"^🗣 (?P<lang>.*?) \| ⭐ (?P<total>[\d,]+)\(今日 \+(?P<today>[\d,]+)\)(?P<rest>.*)$", re.M)
# | [owner/repo](url) | 2 | +827 | 一句話 |   (舊版 4 欄;新版 5 欄多一個累計天數)
CACHED_RE = re.compile(
    r"^\| \[([^\]]+)\]\((https://github\.com/[^)]+)\) \| (\d+) \|(?: (\d+) \|)? \+([\d,]+) \| (.*?) \|$", re.M
)
SCANNED_RE = re.compile(r"掃描到 (\d+) 個上榜專案")
COST_RE = re.compile(r"成本約 \$([\d.]+)")


def _int(text: str) -> int:
    try:
        return int(text.replace(",", ""))
    except (AttributeError, ValueError):
        return 0


@dataclass
class ParsedReport:
    date: str
    analyzed: list[dict] = field(default_factory=list)
    cached: list[dict] = field(default_factory=list)
    declared_scanned: int = 0
    cost: float = 0.0

    @property
    def represented(self) -> int:
        return len(self.analyzed) + len(self.cached)


def parse_report(path: Path) -> ParsedReport:
    """從舊報告還原出當天的分區內容。新舊版面都能解析。"""
    text = path.read_text(encoding="utf-8")
    out = ParsedReport(date=path.stem)

    scanned = SCANNED_RE.search(text)
    if scanned:
        out.declared_scanned = int(scanned.group(1))
    cost = COST_RE.search(text)
    if cost:
        try:
            out.cost = float(cost.group(1))
        except ValueError:
            pass

    # 每個分析區塊:標題後緊接的 meta 行提供語言與 star 數
    blocks = list(BLOCK_RE.finditer(text))
    for i, m in enumerate(blocks):
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
        body = text[m.start():end]
        meta = META_RE.search(body)
        out.analyzed.append({
            "full_name": m.group(1),
            "url": m.group(2),
            "rating": len(m.group(3)),
            "language": (meta.group("lang").strip() if meta else ""),
            "stars_total": _int(meta.group("total")) if meta else 0,
            "stars_today": _int(meta.group("today")) if meta else 0,
            "light": bool(meta and "輕量分析" in meta.group("rest")),
        })

    for m in CACHED_RE.finditer(text):
        out.cached.append({
            "full_name": m.group(1),
            "url": m.group(2),
            "days": int(m.group(3)),
            "stars_today": _int(m.group(5)),
            "one_liner": m.group(6).replace("\\|", "|").strip(),
        })
    return out


def build(parsed: ParsedReport, store: SeenStore) -> tuple[list[RepoResult], list[CachedEntry], list[str]]:
    results: list[RepoResult] = []
    problems: list[str] = []

    for item in parsed.analyzed:
        name = item["full_name"]
        analysis = store.cached_analysis(name)
        if not analysis:
            problems.append(f"{name} 沒有快取分析,無法重繪其內容")
            continue
        language = item["language"] or ""
        if language in ("—", "-"):
            language = ""
        results.append(RepoResult(
            repo=TrendingRepo(
                full_name=name, url=item["url"], description="",
                language=language, stars_total=item["stars_total"],
                stars_today=item["stars_today"], rank=len(results) + 1,
            ),
            status="light" if (analysis.get("_mode") == "light" or item["light"]) else "analyzed",
            analysis=analysis,
            days_on_trending=store.days_on_trending_at(name, parsed.date),
            total_days_on_trending=store.total_days_on_trending_at(name, parsed.date),
        ))

    cached_entries = [
        CachedEntry(
            full_name=item["full_name"], url=item["url"],
            days_on_trending=store.days_on_trending_at(item["full_name"], parsed.date) or item["days"],
            total_days_on_trending=store.total_days_on_trending_at(item["full_name"], parsed.date),
            stars_today=item["stars_today"],
            one_liner=item["one_liner"],
        )
        for item in parsed.cached
    ]
    return results, cached_entries, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="以現行版面重繪既有報告")
    ap.add_argument("dates", nargs="*", help="要重繪的日期(預設全部)")
    ap.add_argument("--check", action="store_true", help="只檢查,不寫檔")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log = logging.getLogger("rerender")

    reports_dir = ROOT / "reports"
    paths = sorted(reports_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
    if args.dates:
        wanted = set(args.dates)
        paths = [p for p in paths if p.stem in wanted]
        missing = wanted - {p.stem for p in paths}
        for date in sorted(missing):
            print(f"找不到報告:{date}", file=sys.stderr)
    if not paths:
        print("沒有可重繪的報告", file=sys.stderr)
        return 1

    store = SeenStore(ROOT / "data" / "seen_repos.json", log)
    store.load()

    failures = 0
    for path in paths:
        parsed = parse_report(path)
        results, cached_entries, problems = build(parsed, store)
        for problem in problems:
            print(f"  !! {problem}")
            failures += 1

        total = len(results) + len(cached_entries)
        note = ""
        if parsed.declared_scanned and parsed.declared_scanned != total:
            # 舊版有「掃描數 > 實際列出」的漏列 bug;重繪只能忠實反映實際內容
            note = f"(原宣告掃描 {parsed.declared_scanned} → 校正為 {total})"

        if args.check:
            print(f"  {parsed.date}:可重繪 分析 {len(results)}、持續上榜 {len(cached_entries)} {note}")
            continue

        render_report(parsed.date, results, cached_entries, parsed.cost,
                      reports_dir, log, total_scanned=total)
        print(f"  {parsed.date}:已重繪 分析 {len(results)}、持續上榜 {len(cached_entries)} {note}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
