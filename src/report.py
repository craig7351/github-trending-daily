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
    "# GitHub Trending 報告索引\n\n"
    "| 日期 | 分析數 | 持續上榜 | 本日之星 |\n"
    "|---|---|---|---|\n"
)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_str_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _rating(r: RepoResult) -> int:
    if isinstance(r.analysis, dict):
        return max(0, min(5, _as_int(r.analysis.get("star_rating"))))
    return 0


def _star_bar(rating: int) -> str:
    rating = max(0, min(5, rating))
    return "★" * rating + "☆" * (5 - rating)


def _table_cell(text: str) -> str:
    # 表格儲存格不能含管線與換行,否則整列會爛掉
    return " ".join(_as_str(text).split()).replace("|", "\\|")


def _short_reason(r: RepoResult) -> str:
    msg = (r.error_msg or "").strip()
    if msg:
        return msg.splitlines()[0][:60]
    return _STATUS_REASONS.get(r.status, "原因不明")


def _meta_line(r: RepoResult, category: str) -> str:
    lang = r.repo.language or "—"
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

    lines = [f"### [{r.repo.full_name}]({r.repo.url}) {_star_bar(_rating(r))}", ""]
    category = _as_str(a.get("category")).strip() or "未分類"
    lines += [_meta_line(r, category), ""]

    summary = _as_str(a.get("summary")).strip()
    if summary:
        lines += [f"**這是什麼:** {summary}", ""]

    highlights = _as_str_list(a.get("highlights"))
    if highlights:
        lines.append("**亮點:**")
        lines += [f"- {h}" for h in highlights]
        lines.append("")

    use_cases = _as_str_list(a.get("use_cases"))
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
        comment = _as_str(quality.get("comment")).strip()
        if comment:
            q_line += f" — {comment}"
        lines += [q_line, ""]

    security = a.get("security")
    if isinstance(security, dict):
        level = _as_str(security.get("risk_level")).strip().lower()
        lines.append(f"**安全:** {_RISK_LABELS.get(level, '❓ 未知')}")
        lines += [f"- {f}" for f in _as_str_list(security.get("findings"))]
        lines.append("")

    verdict = _as_str(a.get("verdict")).strip()
    if verdict:
        lines += [f"**結論:** {verdict}", ""]
    return lines


def _failure_block(r: RepoResult, reason: str | None = None) -> list[str]:
    stars = r.meta.stars if (r.meta.fetched and r.meta.stars) else r.repo.stars_total
    lang = r.repo.language or "—"
    lines = [
        f"### [{r.repo.full_name}]({r.repo.url})",
        "",
        f"🗣 {lang} | ⭐ {stars:,}(今日 +{r.repo.stars_today:,})"
        f"| 上榜第 {r.days_on_trending} 天",
        "",
    ]
    desc = (r.repo.description or "").strip()
    if desc:
        lines += [desc, ""]
    lines += [f"_AI 分析未完成({reason or _short_reason(r)}),僅列基本資訊。_", ""]
    return lines


def render_report(
    run_date: str,
    results: list[RepoResult],
    cached: list[CachedEntry],
    total_cost_usd: float,
    report_dir: Path,
    log: logging.Logger,
    total_scanned: int | None = None,
) -> Path:
    """渲染完整每日報告,回傳報告檔路徑。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"

    success = [r for r in results if r.status in _SUCCESS_STATUSES]
    failed = [r for r in results if r.status not in _SUCCESS_STATUSES]
    analyzed_n = sum(1 for r in success if r.status == "analyzed")
    light_n = len(success) - analyzed_n

    lines: list[str] = [f"# 📈 GitHub Trending 每日報告 — {run_date}", ""]

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
            c = _as_str(r.analysis.get("category")).strip()
            if c:
                cats[c] += 1
    if cats:
        lines.append("- 分類:" + "、".join(f"{c} ×{n}" for c, n in cats.most_common()))

    ranked = sorted(success, key=lambda r: (-_rating(r), -r.repo.stars_today))
    top = [r for r in ranked if _rating(r) > 0][:3]
    if top:
        picks = "、".join(
            f"[{r.repo.full_name}]({r.repo.url})(★{_rating(r)})" for r in top
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
                f"| [{c.full_name}]({c.url}) | {c.days_on_trending} "
                f"| +{c.stars_today:,} | {one} |"
            )
        lines.append("")

    footer = "本報告由 AI 自動產生,分析對象為未經驗證的第三方程式碼,內容僅供參考"
    if total_cost_usd > 0:
        footer += f";本次 API 成本約 ${total_cost_usd:.2f}"
    lines += ["---", "", f"_{footer}。_", ""]

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
        f"# 📈 GitHub Trending 每日報告 — {run_date}\n\n"
        "## ⚠️ 本日掃描失敗\n\n"
        f"{reason}\n\n"
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
    report_dir: Path,
    log: logging.Logger,
) -> None:
    """在 index.md 表頭分隔線後插入(或取代)當日列,最新在最上面。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    index = report_dir / "index.md"
    row = f"| [{run_date}]({run_date}.md) | {analyzed_count} | {cached_count} | {top_pick} |"

    if not index.exists():
        with open(index, "w", encoding="utf-8", newline="\n") as f:
            f.write(_INDEX_HEADER + row + "\n")
        log.info("建立報告索引並加入 %s", run_date)
        return

    with open(index, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    marker = f"| [{run_date}]("
    replaced = any(line.startswith(marker) for line in lines)
    lines = [line for line in lines if not line.startswith(marker)]

    sep_idx = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("|---")), None
    )
    if sep_idx is None:
        # 表頭遺失/格式異常:保留看起來像資料列的部分,整檔重建
        rows = [line for line in lines if line.startswith("| [")]
        content = _INDEX_HEADER + row + "\n"
        if rows:
            content += "\n".join(rows) + "\n"
        log.warning("index.md 缺少表頭分隔線,已重建")
    else:
        lines.insert(sep_idx + 1, row)
        content = "\n".join(lines) + "\n"

    with open(index, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    log.info("索引已%s %s 的列", "更新" if replaced else "加入", run_date)
