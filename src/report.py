"""Render the public GitHub Trending report pages and homepage.

The generated files use Jekyll front matter plus semantic HTML. All content
originating from repositories or AI output is escaped before insertion.
"""
from __future__ import annotations

import html
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

from .models import CachedEntry, RepoResult
from .util import atomic_write_json

_SUCCESS_STATUSES = ("analyzed", "light")

_RISK_LABELS = {
    "none": "無明顯風險",
    "low": "低風險",
    "medium": "中度留意",
    "high": "高度留意",
}

_STATUS_REASONS = {
    "metadata_only": "分析未執行或失敗",
    "clone_failed": "clone 失敗",
    "error": "發生未預期錯誤",
    "deferred": "超過本次分析數量上限",
}

_BACK_LINK = '<a class="text-link" href="../">所有報告</a>'

INDEX_DATA_REL = "data/report_index.json"


def _yaml_quoted(value: str) -> str:
    """Escape a value for a YAML double-quoted scalar.

    HTML escaping is the wrong tool here: front matter is parsed as YAML, where
    a bare ``"`` or ``\\`` would break the scalar (and ``&lt;`` needs no escaping).
    """
    return _as_str(value).replace("\\", "\\\\").replace('"', '\\"')


def _front_matter(title: str) -> list[str]:
    """Return the Jekyll front matter required to render a Markdown file."""
    return ["---", "layout: default", f'title: "{_yaml_quoted(title)}"', "---", ""]


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


_UNSAFE_CHARS = {
    "<": "&lt;",
    ">": "&gt;",
    "[": "&#91;",
    "]": "&#93;",
}


def _safe(value, one_line: bool = True) -> str:
    """Neutralize HTML and Markdown syntax in untrusted public text.

    This helper remains available for older migration scripts and tests.
    New HTML templates use ``_html`` below, which also escapes attributes.
    """
    text = _as_str(value)
    if one_line:
        text = " ".join(text.split())
    for unsafe, escaped in _UNSAFE_CHARS.items():
        text = text.replace(unsafe, escaped)
    return text.strip()


def _html(value, one_line: bool = True) -> str:
    text = _as_str(value)
    if one_line:
        text = " ".join(text.split())
    return html.escape(text.strip(), quote=True)


def _html_list(value) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return []
    return [item for item in (_html(item) for item in items) if item]


def _rating(result: RepoResult) -> int:
    if isinstance(result.analysis, dict):
        return max(0, min(5, _as_int(result.analysis.get("star_rating"))))
    return 0


def _star_bar(rating: int) -> str:
    value = max(0, min(5, rating))
    return "★" * value + "☆" * (5 - value)


def _short_reason(result: RepoResult) -> str:
    message = (result.error_msg or "").strip()
    if message:
        return message.splitlines()[0][:60]
    return _STATUS_REASONS.get(result.status, "原因不明")


def _repo_url(result: RepoResult) -> str:
    url = _as_str(result.repo.url)
    if not url.startswith("https://github.com/"):
        return "https://github.com/"
    return _html(url)


def _copy_section(title: str, text: str = "", items: list[str] | None = None) -> list[str]:
    lines = ['<section class="copy-section">', f"<h3>{title}</h3>"]
    if text:
        lines.append(f"<p>{text}</p>")
    if items:
        lines.append("<ul>")
        lines.extend(f"<li>{item}</li>" for item in items)
        lines.append("</ul>")
    lines.append("</section>")
    return lines


def _quality_module(quality: dict) -> list[str]:
    labels = (("文件", "docs"), ("測試", "tests"), ("活躍度", "activity"))
    lines = ['<section class="aside-module">', "<h3>品質訊號</h3>"]
    for label, key in labels:
        score = max(0, min(5, _as_int(quality.get(key))))
        lines += [
            '<div class="quality-row">',
            '<div class="quality-row__top">',
            f"<span>{label}</span><span>{score}/5</span>",
            "</div>",
            (
                f'<div class="quality-bar" role="meter" aria-label="{label}" '
                f'aria-valuemin="0" aria-valuemax="5" aria-valuenow="{score}">'
                f'<span style="width:{score * 20}%"></span></div>'
            ),
            "</div>",
        ]
    comment = _html(quality.get("comment"))
    if comment:
        lines.append(f'<p class="aside-module__comment">{comment}</p>')
    lines.append("</section>")
    return lines


def _security_module(security: dict) -> list[str]:
    level = _as_str(security.get("risk_level")).lower()
    label = _RISK_LABELS.get(level, "資訊不足")
    css_level = level if level in _RISK_LABELS else "unknown"
    findings = _html_list(security.get("findings"))
    lines = [
        '<section class="aside-module">',
        "<h3>安全觀察</h3>",
        f'<div class="risk-label risk-label--{css_level}">{label}</div>',
    ]
    if findings:
        lines.append('<ul class="risk-findings">')
        lines.extend(f"<li>{finding}</li>" for finding in findings)
        lines.append("</ul>")
    else:
        lines.append('<p class="aside-module__comment">靜態閱讀時未發現需要特別標記的項目。</p>')
    lines.append("</section>")
    return lines


def _success_block(result: RepoResult, featured: bool = False) -> list[str]:
    analysis = result.analysis
    if not isinstance(analysis, dict):
        raise ValueError("analysis 不是 dict")

    name = _html(result.repo.full_name)
    rating = _rating(result)
    category = _html(analysis.get("category")) or "未分類"
    language = _html(result.repo.language) or "未標示語言"
    one_liner = _html(analysis.get("one_liner"))
    summary = _html(analysis.get("summary"))
    highlights = _html_list(analysis.get("highlights"))
    use_cases = _html_list(analysis.get("use_cases"))
    classes = "repo-article repo-article--featured" if featured else "repo-article"

    lines = [
        (
            f'<article class="{classes}" data-repo="{name}" '
            f'data-rating="{rating}">'
        ),
        '<header class="repo-head">',
    ]
    if featured:
        lines.append('<div class="repo-head__label">本日精選</div>')
    lines += [
        '<div class="repo-head__title-row">',
        f'<h2><a href="{_repo_url(result)}">{name}</a></h2>',
        f'<div class="repo-rating" aria-label="{rating} 顆星">{_star_bar(rating)}</div>',
        "</div>",
    ]
    if one_liner:
        lines.append(f'<p class="repo-head__summary">{one_liner}</p>')
    lines += [
        '<div class="repo-meta">',
        f'<span class="pill">{language}</span>',
        f'<span class="pill">累積 {result.repo.stars_total:,} stars</span>',
        f'<span class="pill pill--blue">今日 +{result.repo.stars_today:,}</span>',
        f'<span class="pill">{category}</span>',
        (
            f'<span class="pill">連續 {result.days_on_trending} 天 · '
            f'累計 {result.total_days_on_trending} 天</span>'
        ),
    ]
    if result.status == "light":
        lines.append('<span class="pill">輕量分析</span>')
    lines += ["</div>", "</header>", '<div class="repo-body">', '<div class="repo-copy">']

    if summary:
        lines += _copy_section("這是什麼", text=summary)
    if highlights:
        lines += _copy_section("值得注意的亮點", items=highlights)
    if use_cases:
        lines += _copy_section("適合誰使用", items=use_cases)
    if not summary and not highlights and not use_cases:
        lines += _copy_section("分析摘要", text="目前沒有可顯示的內容。")

    lines += ["</div>", '<aside class="repo-aside">']
    quality = analysis.get("quality")
    if isinstance(quality, dict):
        lines += _quality_module(quality)
    security = analysis.get("security")
    if isinstance(security, dict):
        lines += _security_module(security)
    verdict = _html(analysis.get("verdict"))
    if verdict:
        lines += [
            '<section class="aside-module">',
            "<h3>快速結論</h3>",
            f'<p class="verdict">{verdict}</p>',
            "</section>",
        ]
    lines += ["</aside>", "</div>", "</article>"]
    return lines


def _failure_block(result: RepoResult, reason: str | None = None) -> list[str]:
    stars = (
        result.meta.stars
        if result.meta.fetched and result.meta.stars
        else result.repo.stars_total
    )
    description = _html(result.repo.description)
    detail = _html(reason or _short_reason(result))
    return [
        '<article class="repo-basic">',
        "<div>",
        f'<h3><a href="{_repo_url(result)}">{_html(result.repo.full_name)}</a></h3>',
        (
            f"<p>{description}</p>" if description else
            "<p>目前沒有可顯示的專案描述。</p>"
        ),
        "</div>",
        (
            '<div class="repo-basic__meta">'
            f'<span class="pill">{_html(result.repo.language) or "未標示語言"}</span> '
            f'<span class="pill">★ {stars:,} · 今日 +{result.repo.stars_today:,}</span>'
            f'<p>AI 分析未完成：{detail}</p>'
            "</div>"
        ),
        "</article>",
    ]


def _stat(label: str, value: int) -> list[str]:
    return [
        '<div class="report-stat">',
        f'<span class="report-stat__label">{label}</span>',
        f'<span class="report-stat__value">{value}</span>',
        "</div>",
    ]


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
    """Render a complete daily report and return its path."""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"

    success = [result for result in results if result.status in _SUCCESS_STATUSES]
    deferred = [result for result in results if result.status == "deferred"]
    failed = [
        result for result in results
        if result.status not in _SUCCESS_STATUSES and result.status != "deferred"
    ]
    fresh_success = [result for result in success if not result.from_cache]
    replayed_success = [result for result in success if result.from_cache]
    full_count = sum(result.status == "analyzed" for result in success)
    light_count = len(success) - full_count
    scanned = total_scanned if total_scanned is not None else len(results) + len(cached)
    represented = len(results) + len(cached)
    if scanned != represented:
        raise ValueError(f"報告數量不守恆：掃描 {scanned}，但分類後只有 {represented}")

    ranked = sorted(success, key=lambda item: (-_rating(item), -item.repo.stars_today))
    featured = ranked[0] if ranked and _rating(ranked[0]) > 0 else None

    lines = _front_matter(f"GitHub Trending 報告 — {run_date}")
    lines += [
        '<div class="report-shell">',
        '<nav class="breadcrumb" aria-label="麵包屑">',
        _BACK_LINK,
        '<span aria-hidden="true">/</span>',
        f'<span class="mono">{_html(run_date)}</span>',
        "</nav>",
        '<header class="report-header">',
        '<span class="eyebrow">Daily intelligence report</span>',
        f"<h1>GitHub Trending 每日報告</h1>",
        f'<p class="report-header__meta"><span class="mono">{_html(run_date)}</span>'
        " · 只讀原始碼的 AI 靜態分析</p>",
        "</header>",
    ]
    if backfilled_on:
        lines += [
            '<aside class="ai-note">',
            '<span class="ai-note__mark">i</span>',
            (
                f"<span>這是事後補跑報告，產生於 {_html(backfilled_on)}。榜單與 star 數為 "
                f"{_html(run_date)} 當日紀錄，分析內容則來自補跑當下的專案版本。</span>"
            ),
            "</aside>",
        ]

    lines.append(
        (
            f'<section class="report-stats" aria-label="報告總覽" '
            f'data-scanned="{scanned}" data-analyzed="{len(success)}" '
            f'data-full="{full_count}" data-light="{light_count}" '
            f'data-cached="{len(cached)}" data-replayed="{len(replayed_success)}" '
            f'data-deferred="{len(deferred)}" data-failed="{len(failed)}">'
        )
    )
    lines += _stat("掃描專案", scanned)
    lines += _stat("完整分析", full_count)
    lines += _stat("輕量分析", light_count)
    lines += _stat("同日保留", len(replayed_success))
    lines += _stat("持續上榜", len(cached))
    lines += _stat("待分析／失敗", len(deferred) + len(failed))
    lines.append("</section>")

    categories: Counter[str] = Counter()
    for result in success:
        if isinstance(result.analysis, dict):
            category = _as_str(result.analysis.get("category")).strip()
            if category:
                categories[category] += 1
    if categories:
        category_text = " · ".join(
            f"{_html(category)} ×{count}"
            for category, count in categories.most_common()
        )
        lines += [
            '<div class="ai-note">',
            '<span class="ai-note__mark">i</span>',
            f"<span>本日分類：{category_text}</span>",
            "</div>",
        ]

    if fresh_success:
        lines += [
            '<section class="report-section">',
            '<div class="report-section__title"><h2>本次完成分析</h2></div>',
        ]
        ordered_fresh = sorted(
            fresh_success, key=lambda item: (-_rating(item), -item.repo.stars_today)
        )
        for result in ordered_fresh:
            try:
                lines += _success_block(result, featured=result is featured)
            except Exception as exc:
                log.warning(
                    "渲染 %s 分析區塊失敗，降級為基本資訊：%s",
                    result.repo.full_name,
                    exc,
                )
                lines += _failure_block(result, "分析結果格式異常")
        lines.append("</section>")
    else:
        lines += [
            '<section class="report-section">',
            '<div class="report-section__title"><h2>本次完成分析</h2></div>',
            '<p class="muted">本次沒有新完成的分析。</p>',
            "</section>",
        ]

    if replayed_success:
        heading = "快取分析結果" if backfilled_on else "今日稍早已分析"
        lines += [
            '<section class="report-section">',
            f'<div class="report-section__title"><h2>{heading}</h2></div>',
        ]
        for result in replayed_success:
            try:
                lines += _success_block(result, featured=result is featured)
            except Exception as exc:
                log.warning(
                    "渲染 %s 快取分析失敗，降級為基本資訊：%s",
                    result.repo.full_name,
                    exc,
                )
                lines += _failure_block(result, "快取分析格式異常")
        lines.append("</section>")

    if failed:
        lines += [
            '<section class="report-section">',
            '<div class="report-section__title"><h2>降級或失敗</h2></div>',
        ]
        for result in failed:
            try:
                lines += _failure_block(result)
            except Exception as exc:
                log.error("渲染 %s 基本資訊失敗，略過：%s", result.repo.full_name, exc)
        lines.append("</section>")

    if deferred:
        lines += [
            '<section class="report-section">',
            '<div class="report-section__title"><h2>尚待分析</h2></div>',
            '<p class="muted">以下專案超過本次分析數量上限；若後續仍在榜會再嘗試。</p>',
        ]
        for result in deferred:
            try:
                lines += _failure_block(result)
            except Exception as exc:
                log.error("渲染 %s 待分析資訊失敗，略過：%s", result.repo.full_name, exc)
        lines.append("</section>")

    if cached:
        lines += [
            '<section class="report-section">',
            '<div class="report-section__title"><h2>持續上榜</h2></div>',
            '<div class="cached-list">',
        ]
        for entry in cached:
            url = _as_str(entry.url)
            safe_url = _html(url) if url.startswith("https://github.com/") else "https://github.com/"
            lines += [
                '<div class="cached-row">',
                (
                    f'<a class="cached-row__name" href="{safe_url}">'
                    f"{_html(entry.full_name)}</a>"
                ),
                f'<span class="cached-row__metric">連續 {entry.days_on_trending} 天</span>',
                f'<span class="cached-row__metric">累計 {entry.total_days_on_trending} 天</span>',
                f'<span class="cached-row__metric">今日 +{entry.stars_today:,}</span>',
                (
                    f'<span class="cached-row__summary">'
                    f'{_html(entry.one_liner) or "暫無摘要"}</span>'
                ),
                "</div>",
            ]
        lines += ["</div>", "</section>"]

    lines += [
        '<footer class="report-disclaimer">',
        "<h2>關於這份分析</h2>",
        (
            "<p>本報告由 AI 自動產生，未經人工審閱或驗證。系統只靜態閱讀公開的 README "
            "與原始碼，不執行任何專案程式；內容可能有誤解、過時或不完整之處。</p>"
        ),
        (
            "<p>安全觀察只記錄靜態閱讀時值得留意的線索，不構成安全稽核結論，也不代表"
            "專案存在惡意或缺陷。實際採用前，請以官方文件與原始碼為準。</p>"
        ),
    ]
    if total_cost_usd > 0:
        lines.append(
            f'<p class="mono">本次分析 API 名目成本約 ${total_cost_usd:.2f}</p>'
        )
    lines += ["</footer>", "</div>"]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log.info(
        "報告已寫入：%s（成功 %d、失敗 %d、持續上榜 %d、待分析 %d）",
        path,
        len(success),
        len(failed),
        len(cached),
        len(deferred),
    )
    return path


def render_stub_report(
    run_date: str, reason: str, report_dir: Path, log: logging.Logger
) -> Path:
    """Write a presentable fallback page when the entire scan fails."""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_date}.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = _front_matter(f"GitHub Trending 報告 — {run_date}")
    lines += [
        '<div class="report-shell">',
        '<nav class="breadcrumb" aria-label="麵包屑">',
        _BACK_LINK,
        '<span aria-hidden="true">/</span>',
        f'<span class="mono">{_html(run_date)}</span>',
        "</nav>",
        '<header class="report-header">',
        '<span class="eyebrow">Daily intelligence report</span>',
        "<h1>GitHub Trending 每日報告</h1>",
        f'<p class="report-header__meta mono">{_html(run_date)}</p>',
        "</header>",
        '<section class="report-section">',
        '<div class="report-section__title"><h2>本日掃描失敗</h2></div>',
        f"<p>{_html(reason)}</p>",
        f'<p class="muted">產生時間：{timestamp}；詳情請查看當日 log。</p>',
        "</section>",
        "</div>",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log.warning("已寫入失敗替代報告：%s（原因：%s）", path, reason)
    return path


def _load_index_rows(data_file: Path, log: logging.Logger) -> list[dict]:
    if not data_file.exists():
        return []
    try:
        rows = json.loads(data_file.read_text(encoding="utf-8-sig"))
        return rows if isinstance(rows, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("索引資料讀取失敗（%s），以空白重建", exc)
        return []


def _top_text(row: dict) -> str:
    name = _html(row.get("top_name"))
    if not name:
        return ""
    rating = _as_int(row.get("top_rating"))
    return f"{name}" + (f" · {_star_bar(rating)}" if rating else "")


def _render_index(rows: list[dict], report_subdir: str) -> str:
    """Render the homepage from newest to oldest report index rows."""
    report_root = _html(report_subdir.strip("/")) or "reports"
    out = _front_matter("GitHub Trending 每日觀察")
    out += [
        '<section class="home-intro">',
        '<div class="home-intro__copy">',
        '<span class="eyebrow">Open-source signal, distilled daily</span>',
        "<h1>掌握今天值得注意的<br>GitHub 專案</h1>",
        (
            "<p>每天掃描 GitHub Trending，以只讀方式分析新上榜專案，"
            "把亮點、適用場景、品質與安全訊號整理成繁體中文。</p>"
        ),
        "</div>",
        "</section>",
    ]

    if rows:
        latest = rows[0]
        date = _html(latest.get("date"))
        analyzed = _as_int(latest.get("analyzed"))
        cached = _as_int(latest.get("cached"))
        deferred = _as_int(latest.get("deferred"))
        top_name = _html(latest.get("top_name")) or "本日推薦整理中"
        top_rating = max(0, min(5, _as_int(latest.get("top_rating"))))
        out += [
            '<section class="home-feature" aria-label="最新報告">',
            '<div class="latest-report">',
            '<span class="eyebrow">Latest report</span>',
            f'<div class="latest-report__date">{date}</div>',
            (
                f'<p class="latest-report__summary">完成 {analyzed} 個專案分析'
                + (f"，另有 {cached} 個持續上榜" if cached else "")
                + "。</p>"
            ),
            f'<a class="text-link" href="{report_root}/{date}.html">閱讀完整報告</a>',
            '<div class="metric-row">',
            (
                '<div class="metric"><span class="metric__label">完成分析</span>'
                f'<span class="metric__value">{analyzed}</span></div>'
            ),
            (
                '<div class="metric"><span class="metric__label">持續上榜</span>'
                f'<span class="metric__value">{cached}</span></div>'
            ),
            (
                '<div class="metric"><span class="metric__label">待分析</span>'
                f'<span class="metric__value">{deferred}</span></div>'
            ),
            "</div>",
            "</div>",
            '<div class="top-project">',
            '<span class="eyebrow">Project of the day</span>',
            f'<h2 class="top-project__name">{top_name}</h2>',
            (
                f'<div class="top-project__rating" aria-label="{top_rating} 顆星">'
                f"{_star_bar(top_rating)}</div>"
            ),
            (
                "<p class=\"muted\">依分析評分與當日關注度選出的快速入口。"
                "完整報告包含專案用途、亮點、品質與安全觀察。</p>"
            ),
            '<div class="top-project__meta">',
            '<span class="pill pill--blue">AI 靜態分析</span>',
            '<span class="pill">不執行程式碼</span>',
            "</div>",
            "</div>",
            "</section>",
        ]

    out += [
        '<aside class="ai-note">',
        '<span class="ai-note__mark">i</span>',
        (
            '<span>內容由 AI 自動產生，未經人工審閱或驗證，僅供快速篩選參考。'
            '<a href="#disclaimer">閱讀免責聲明</a></span>'
        ),
        "</aside>",
        '<section id="reports">',
        '<div class="section-heading">',
        "<div>",
        "<h2>每日報告</h2>",
        f"<p>共 {len(rows)} 份，最新報告置頂。</p>",
        "</div>",
        "</div>",
        '<div class="report-list">',
    ]
    if rows:
        for row in rows:
            date = _html(row.get("date"))
            analyzed = _as_int(row.get("analyzed"))
            deferred = _as_int(row.get("deferred"))
            top = _top_text(row) or "本日推薦整理中"
            count_text = f"分析 {analyzed} 個"
            if deferred:
                count_text += f" · 待分析 {deferred}"
            out += [
                f'<a class="report-row" href="{report_root}/{date}.html">',
                f'<span class="report-row__date">{date}</span>',
                f'<span class="report-row__count">{count_text}</span>',
                f'<span class="report-row__top">{top}</span>',
                '<span class="report-row__arrow" aria-hidden="true">→</span>',
                "</a>",
            ]
    else:
        out.append('<p class="muted">目前還沒有報告。</p>')
    out += [
        "</div>",
        "</section>",
        '<div class="info-grid">',
        '<section class="info-section" id="about">',
        "<h2>GitHub Trending 看的是什麼？</h2>",
        (
            '<p><a href="https://github.com/trending">GitHub Trending</a> '
            "呈現近期快速獲得關注的公開專案。它反映的是關注度變化，不等於品質、"
            "成熟度或長期價值。</p>"
        ),
        (
            "<p>本站每天掃描每日榜，針對新進榜專案閱讀公開 README 與原始碼，"
            "先回答「這是什麼」與「值不值得花時間深入了解」。</p>"
        ),
        '<div class="info-callout">榜單熱度是線索，不是結論；實際採用前仍應查閱官方文件與原始碼。</div>',
        "</section>",
        '<section class="info-section" id="disclaimer">',
        "<h2>免責聲明</h2>",
        (
            "<p>所有分析由 AI 自動產生，未經人工審閱。系統不執行專案程式碼，"
            "仍可能誤解、遺漏或引用過時資訊。</p>"
        ),
        (
            "<p>安全觀察不構成安全稽核，也不表示專案存在惡意或缺陷；"
            "評分與結論僅供快速篩選。</p>"
        ),
        "</section>",
        "</div>",
    ]
    return "\n".join(out) + "\n"


def update_index(
    run_date: str,
    analyzed_count: int,
    cached_count: int,
    top: tuple[str, int] | None,
    index_dir: Path,
    log: logging.Logger,
    report_subdir: str = "reports",
    deferred_count: int = 0,
) -> None:
    """Update the report data and fully redraw the site homepage."""
    index_dir.mkdir(parents=True, exist_ok=True)
    data_file = index_dir / INDEX_DATA_REL
    existing_rows = _load_index_rows(data_file, log)
    rows = [
        row for row in existing_rows
        if isinstance(row, dict) and row.get("date") != run_date
    ]
    existed = len(rows) != len(existing_rows)
    rows.append({
        "date": run_date,
        "analyzed": analyzed_count,
        "cached": cached_count,
        "deferred": deferred_count,
        "top_name": top[0] if top else "",
        "top_rating": top[1] if top else 0,
    })
    rows.sort(key=lambda row: str(row.get("date", "")), reverse=True)

    atomic_write_json(data_file, rows)
    (index_dir / "index.md").write_text(
        _render_index(rows, report_subdir), encoding="utf-8", newline="\n"
    )
    log.info(
        "首頁索引已%s %s（共 %d 份報告）",
        "更新" if existed else "加入",
        run_date,
        len(rows),
    )
