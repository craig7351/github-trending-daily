"""一次性遷移:為既有報告補上 Jekyll front matter 與返回索引連結。

在 GitHub Pages 上,沒有 front matter 的 .md 不會被轉成 HTML。
本腳本可重複執行(已處理過的檔案會跳過)。

用法:python scripts/migrate_reports_to_pages.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def migrate(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\nlayout:"):
        return "已處理,跳過"

    date = path.stem
    front = f'---\nlayout: default\ntitle: "GitHub Trending 報告 — {date}"\n---\n\n'

    # 在第一個 H1 之後插入返回連結
    lines = text.splitlines()
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.startswith("# "):
            out += ["", "[← 回到報告索引](../)"]
            inserted = True
    body = "\n".join(out)
    if not body.endswith("\n"):
        body += "\n"

    path.write_text(front + body, encoding="utf-8", newline="\n")
    return "已加入 front matter" + ("與返回連結" if inserted else "")


def main() -> int:
    if not REPORTS.is_dir():
        print(f"找不到報告目錄:{REPORTS}", file=sys.stderr)
        return 1

    targets = sorted(p for p in REPORTS.glob("*.md") if DATE_RE.match(p.name))
    if not targets:
        print("沒有需要遷移的報告")
        return 0

    for p in targets:
        print(f"{p.name}: {migrate(p)}")

    # 舊的 reports/index.md 已被根目錄 index.md 取代
    old_index = REPORTS / "index.md"
    if old_index.exists():
        old_index.unlink()
        print("已移除舊的 reports/index.md(改由根目錄 index.md 當站台首頁)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
