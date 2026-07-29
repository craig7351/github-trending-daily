"""一次性遷移:讓公開發布前產生的舊報告與現行格式一致。

做三件事(皆可重複執行,已處理的會跳過):
1. 補上 Jekyll front matter — Pages 上沒有 front matter 的 .md 不會轉成 HTML
2. 在 H1 後插入返回索引連結
3. 把舊版的一行式聲明換成強化版免責聲明(公開站台需要完整揭露)

用法:python scripts/migrate_reports_to_pages.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

# 舊版一行式聲明,可能帶成本尾綴
OLD_FOOTER_RE = re.compile(
    r"_本報告由 AI 自動產生,分析對象為未經驗證的第三方程式碼,內容僅供參考"
    r"(?:;本次 API 成本約 \$(?P<cost>[\d.]+))?。_"
)

NEW_FOOTER = (
    "### 免責聲明\n\n"
    "本報告的所有分析內容由 AI 自動產生,**未經人工審閱或驗證**。分析方式為靜態閱讀"
    "專案的 README 與原始碼(不執行任何程式碼),因此可能有誤解、過時或不完整之處。\n\n"
    "「安全觀察」一節僅記錄靜態閱讀時值得留意的地方(例如安裝腳本會執行外部指令),"
    "**不構成安全稽核結論,亦不表示該專案存在惡意或缺陷**。評分與結論屬主觀判斷,"
    "僅供快速篩選參考,實際評估請以各專案的官方文件與原始碼為準。\n\n"
    "報告內容擷取自第三方公開 repo,其著作權歸原作者所有。若您是專案維護者且認為"
    "本頁描述有誤,歡迎開 issue 指正。\n"
)


def _upgrade_footer(text: str) -> tuple[str, bool]:
    m = OLD_FOOTER_RE.search(text)
    if not m:
        return text, False
    cost = m.group("cost")
    replacement = NEW_FOOTER
    if cost:
        replacement += f"\n_本次分析 API 名目成本約 ${cost}。_\n"
    return text[: m.start()] + replacement + text[m.end():].lstrip("\n"), True


def migrate(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    done: list[str] = []

    if not text.startswith("---\nlayout:"):
        date = path.stem
        front = f'---\nlayout: default\ntitle: "GitHub Trending 報告 — {date}"\n---\n\n'
        # 在第一個 H1 之後插入返回連結
        out: list[str] = []
        inserted = False
        for line in text.splitlines():
            out.append(line)
            if not inserted and line.startswith("# "):
                out += ["", "[← 回到報告索引](../)"]
                inserted = True
        text = front + "\n".join(out)
        if not text.endswith("\n"):
            text += "\n"
        done.append("front matter")
        if inserted:
            done.append("返回連結")

    text, upgraded = _upgrade_footer(text)
    if upgraded:
        done.append("強化免責聲明")

    if not done:
        return "已是最新格式,跳過"
    path.write_text(text, encoding="utf-8", newline="\n")
    return "已加入:" + "、".join(done)


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
