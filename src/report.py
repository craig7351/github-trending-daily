"""渲染 zh-TW Markdown 每日報告(reports/{date}.md)與索引(reports/index.md)。
所有檔案以 UTF-8、LF 換行寫出;單一 repo 渲染失敗只降級、不中斷整份報告。"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from .models import CachedEntry, RepoResult

_SUCCESS_STATUSES = ("analyzed", "light")

_RISK_LABELS = {
    "none": "✅ 無風險",
    "low": "🟡 低",
    "medium": "🟠 中",
    "high": "🔴 高",
}

_STATUS_REASONS = {
    "metadata_only": "分析未執行或失敗",
    "clone_failed": "clone 失敗",
    "error": "發生未預期錯誤",
}

_INDEX_HEADER = (
    "---\n"
    "layout: default\n"
    "title: GitHub Trending 每日觀察\n"
    "---\n\n"
    "# 📈 GitHub Trending 每日觀察\n\n"
    "每天自動掃描 [GitHub Trending](https://github.com/trending),對新上榜的專案做 "
    "AI 靜態分析(只讀原始碼,不執行),產出繁體中文摘要:這是什麼、亮點、適用場景、"
    "品質與安全觀察。\n\n"
    "## 什麼是 GitHub Trending?\n\n"
    "[GitHub Trending](https://github.com/trending) 是 GitHub 官方的熱門專案榜,"
    "依**最近新增的 star 數**排序,而不是看累積總數 —— 所以榜上常會出現剛發布幾天、"
    "總星數還不高,但正在被大量關注的新專案。可切換 "
    "[今日](https://github.com/trending?since=daily)、"
    "[本週](https://github.com/trending?since=weekly)、"
    "[本月](https://github.com/trending?since=monthly)三種區間,"
    "也能依[程式語言](https://github.com/trending/python)篩選。"
    "GitHub 未公開具體的排名演算法。\n\n"
    "本站掃描的是**每日榜**,每天約 15–25 個專案。\n\n"
    "榜單反映的是「關注度的變化」,不等於品質或成熟度 —— 話題性強的專案、行銷推廣、"
    "或短期被大量轉發的內容都可能上榜。這也是本站做 AI 分析的原因:"
    "讓你在點進去之前,先知道那是什麼、值不值得花時間。\n\n"
    "> ⚠️ **請注意**:所有分析內容皆由 AI 自動產生,**未經人工審閱或驗證**,"
    "可能有誤解、過時或不完整之處。安全觀察僅為靜態閱讀後的提醒,"
    "**不構成安全稽核結論**,也不代表專案存在惡意。請以各專案的官方文件與原始碼為準。\n\n"
    "## 報告索引\n\n"
    "| 日期 | 分析數 | 持續上榜 | 本日之星 |\n"
    "|---|---|---|---|\n"
)


def _front_matter(title: str) -> list[str]:
    """Jekyll 需要 front matter 才會把 .md 轉成 HTML。"""
    return ["---", "layout: default", f'title: "{title}"', "---", ""]


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


# 報告會公開發布,而 AI 產生的文字與 GitHub 描述都源自不可信的第三方 repo。
# 惡意 README 可能誘使模型輸出 HTML 或 Markdown 連結/圖片,渲染在站台上。
# 以下把構成連結與標籤的字元換成 HTML 實體:視覺上一致,但失去語法作用。
_UNSAFE_CHARS = {
    "<": "&lt;",     # HTML 標籤、<script>
    ">": "&gt;",     # 同上;行首的 > 也會變引言區塊
    "[": "&#91;",    # Markdown 連結/圖片語法
    "]": "&#93;",
}


def _safe(value, one_line: bool = True) -> str:
    """淨化不可信來源的文字,供寫入公開 Markdown 用。"""
    s = _as_str(value)
    if one_line:
        s = " ".join(s.split())      # 收掉換行,避免破壞區塊結構
    for bad, good in _UNSAFE_CHARS.items():
        s = s.replace(bad, good)
    return s.strip()


def _safe_list(value) -> list[str]:
    """淨化字串陣列;空字串會被濾掉。"""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(x) for x in value]
    else:
        return []
    out = [_safe(x) for x in items]
    return [x for x in out if x]


def _rating(r: RepoResult) -> int:
    if isinstance(r.analysis, dict):
        return max(0, min(5, _as_int(r.analysis.get("star_rating"))))
    return 0


def _star_bar(rating: int) -> str:
    rating = max(0, min(5, rating))
    return "★" * rating + "☆" * (5 - rating)


def _table_cell(text: str) -> str:
    # 表格儲存格不能含管線,否則整列會爛掉(換行已由 _safe 收掉)
    return _safe(text).replace("|", "\\|")


def _short_reason(r: RepoResult) -> str:
    msg = (r.error_msg or "").strip()
    if msg:
        return msg.splitlines()[0][:60]
    return _STATUS_REASONS.get(r.status, "原因不明")


def _meta_line(r: RepoResult, category: str) -> str:
    lang = _safe(r.repo.language) or "—"
    line = (
        f"🗣 {lang} | ⭐ {r.repo.stars_total:,}(今日 +{r.repo.stars_today:,})"
        f"| 分類:{category} | 上榜第 {r.days_on_trending} 天"
    )
    if r.status == "light":
        line += " |(輕量分析)"
    return line


def _success_block(r: RepoResult) -> list[str]:
    a = r.analysis
    if not isinstance(a, dict):
        raise ValueError("analysis 不是 dict")

    # repo 全名只允許 GitHub 的合法字元,才可安全放進 Markdown 連結
    lines = [f"### [{_safe(r.repo.full_name)}]({r.repo.url}) {_star_bar(_rating(r))}", ""]
    category = _safe(a.get("category")) or "未分類"
    lines += [_meta_line(r, category), ""]

    summary = _safe(a.get("summary"))
    if summary:
        lines += [f"**這是什麼:** {summary}", ""]

    highlights = _safe_list(a.get("highlights"))
    if highlights:
        lines.append("**亮點:**")
        lines += [f"- {h}" for h in highlights]
        lines.append("")

    use_cases = _safe_list(a.get("use_cases"))
    if use_cases:
        lines.append("**適用場景:**")
        lines += [f"- {u}" for u in use_cases]
        lines.append("")

    quality = a.get("quality")
    if isinstance(quality, dict):
        q_line = (
            f"**品質:** 文件 {_as_int(quality.get('docs'))}/5 · "
            f"測試 {_as_int(quality.get('tests'))}/5 · "
            f"活躍度 {_as_int(quality.get('activity'))}/5"
        )
        comment = _safe(quality.get("comment"))
        if comment:
            q_line += f" — {comment}"
        lines += [q_line, ""]

    security = a.get("security")
    if isinstance(security, dict):
        level = _safe(security.get("risk_level")).lower()
        lines.append(f"**安全觀察:** {_RISK_LABELS.get(level, '❓ 未知')}")
        lines += [f"- {f}" for f in _safe_list(security.get("findings"))]
        lines.append("")

    verdict = _safe(a.get("verdict"))
    if verdict:
        lines += [f"**結論:** {verdict}", ""]
    return lines


def _failure_block(r: RepoResult, reason: str | None = None) -> list[str]:
    stars = r.meta.stars if (r.meta.fetched and r.meta.stars) else r.repo.stars_total
    lang = _safe(r.repo.language) or "—"
    lines = [
        f"### [{_safe(r.repo.full_name)}]({r.repo.url})",
        "",
        f"🗣 {lang} | ⭐ {stars:,}(今日 +{r.repo.stars_today:,})"
        f"| 上榜第 {r.days_on_trending} 天",
        "",
    ]
    desc = _safe(r.repo.description)   # GitHub 描述同樣是不可信輸入
    if desc:
        lines += [desc, ""]
    lines += [f"_AI 分析未完成({_safe(reason or _short_reason(r))}),僅列基本資訊。_", ""]
    return lines


def render_report(
    run_date: str,
    results: list[RepoResult],
    cached: list[CachedEntry],
    total_cost_usd: float,
    report_dir: Path,
    log: logging.Logger,
    total_scanned: int | None = None,
    backfilled_on: str = "",
) -> Path:
    """渲染完整每日報告,回傳報告檔路徑。backfilled_on 非空時標注為事後補跑。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"

    success = [r for r in results if r.status in _SUCCESS_STATUSES]
    failed = [r for r in results if r.status not in _SUCCESS_STATUSES]
    analyzed_n = sum(1 for r in success if r.status == "analyzed")
    light_n = len(success) - analyzed_n

    lines: list[str] = _front_matter(f"GitHub Trending 報告 — {run_date}")
    lines += [f"# 📈 GitHub Trending 每日報告 — {run_date}", "",
              "[← 回到報告索引](../)", ""]
    if backfilled_on:
        lines += [
            f"> ⏪ **事後補跑**(產生於 {backfilled_on})。榜單與 star 數為 {run_date} 當日紀錄,"
            "但 AI 分析讀取的是專案在補跑當下的內容,可能與當日狀態略有出入。",
            "",
        ]

    lines += ["## 📊 總覽", ""]
    scanned = total_scanned if total_scanned is not None else len(results) + len(cached)
    lines.append(f"- 掃描到 {scanned} 個上榜專案")
    lines.append(f"- 本日完整分析 {analyzed_n} 個")
    lines.append(f"- 輕量分析 {light_n} 個")
    lines.append(f"- 持續上榜 {len(cached)} 個")
    lines.append(f"- 降級/失敗 {len(failed)} 個")

    cats: Counter[str] = Counter()
    for r in success:
        if isinstance(r.analysis, dict):
            c = _safe(r.analysis.get("category"))
            if c:
                cats[c] += 1
    if cats:
        lines.append("- 分類:" + "、".join(f"{c} ×{n}" for c, n in cats.most_common()))

    ranked = sorted(success, key=lambda r: (-_rating(r), -r.repo.stars_today))
    top = [r for r in ranked if _rating(r) > 0][:3]
    if top:
        picks = "、".join(
            f"[{_safe(r.repo.full_name)}]({r.repo.url})(★{_rating(r)})" for r in top
        )
        lines.append(f"- 推薦榜:{picks}")
    lines.append("")

    lines += ["## 🆕 今日新進榜", ""]
    if not results:
        lines += ["本日無新進榜專案。", ""]
    for r in success:
        try:
            lines += _success_block(r)
        except Exception as e:
            log.warning("渲染 %s 分析區塊失敗,降級為基本資訊:%s", r.repo.full_name, e)
            try:
                lines += _failure_block(r, reason="分析結果格式異常")
            except Exception as e2:
                log.error("渲染 %s 基本資訊也失敗,略過:%s", r.repo.full_name, e2)
    for r in failed:
        try:
            lines += _failure_block(r)
        except Exception as e:
            log.error("渲染 %s 基本資訊失敗,略過:%s", r.repo.full_name, e)

    if cached:
        lines += ["## 🔁 持續上榜", ""]
        lines.append("| 專案 | 連續天數 | 今日新增 | 一句話 |")
        lines.append("|---|---|---|---|")
        for c in cached:
            one = _table_cell(c.one_liner) or "—"
            lines.append(
                f"| [{_safe(c.full_name)}]({c.url}) | {c.days_on_trending} "
                f"| +{c.stars_today:,} | {one} |"
            )
        lines.append("")

    lines += [
        "---", "",
        "### 免責聲明", "",
        "本報告的所有分析內容由 AI 自動產生,**未經人工審閱或驗證**。分析方式為靜態閱讀"
        "專案的 README 與原始碼(不執行任何程式碼),因此可能有誤解、過時或不完整之處。",
        "",
        "「安全觀察」一節僅記錄靜態閱讀時值得留意的地方(例如安裝腳本會執行外部指令),"
        "**不構成安全稽核結論,亦不表示該專案存在惡意或缺陷**。評分與結論屬主觀判斷,"
        "僅供快速篩選參考,實際評估請以各專案的官方文件與原始碼為準。",
        "",
        "報告內容擷取自第三方公開 repo,其著作權歸原作者所有。若您是專案維護者且認為"
        "本頁描述有誤,歡迎開 issue 指正。",
        "",
    ]
    if total_cost_usd > 0:
        lines += [f"_本次分析 API 名目成本約 ${total_cost_usd:.2f}。_", ""]

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    log.info("報告已寫入:%s(成功 %d、失敗 %d、持續上榜 %d)",
             path, len(success), len(failed), len(cached))
    return path


def render_stub_report(
    run_date: str, reason: str, report_dir: Path, log: logging.Logger
) -> Path:
    """掃描整體失敗時寫入替代報告,確保日期不會無聲缺漏。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = (
        "\n".join(_front_matter(f"GitHub Trending 報告 — {run_date}")) + "\n"
        f"# 📈 GitHub Trending 每日報告 — {run_date}\n\n"
        "[← 回到報告索引](../)\n\n"
        "## ⚠️ 本日掃描失敗\n\n"
        f"{_safe(reason)}\n\n"
        f"_產生時間:{ts};詳情請查看當日 log。_\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    log.warning("已寫入失敗替代報告:%s(原因:%s)", path, reason)
    return path


def update_index(
    run_date: str,
    analyzed_count: int,
    cached_count: int,
    top_pick: str,
    index_dir: Path,
    log: logging.Logger,
    report_subdir: str = "reports",
) -> None:
    """更新站台首頁 index.md 的報告索引表,依日期新到舊排序。

    index_dir 為 repo 根目錄(GitHub Pages 的站台根),連結指向 Jekyll 轉出的 .html。"""
    index_dir.mkdir(parents=True, exist_ok=True)
    index = index_dir / "index.md"
    link = f"{report_subdir}/{run_date}.html"
    row = f"| [{run_date}]({link}) | {analyzed_count} | {cached_count} | {top_pick} |"

    if not index.exists():
        with open(index, "w", encoding="utf-8", newline="\n") as f:
            f.write(_INDEX_HEADER + row + "\n")
        log.info("建立報告索引並加入 %s", run_date)
        return

    with open(index, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    marker = f"| [{run_date}]("
    replaced = any(line.startswith(marker) for line in lines)

    # 依日期新到舊重排:補跑舊日期時才不會插在錯的位置
    rows = [ln for ln in lines if ln.startswith("| [") and not ln.startswith(marker)]
    rows.append(row)
    rows.sort(key=lambda ln: ln[3:13], reverse=True)   # "| [YYYY-MM-DD](" 的日期段
    content = _INDEX_HEADER + "\n".join(rows) + "\n"

    if not any(ln.strip().startswith("|---") for ln in lines):
        log.warning("index.md 缺少表頭分隔線,已重建")

    with open(index, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    log.info("索引已%s %s 的列", "更新" if replaced else "加入", run_date)
