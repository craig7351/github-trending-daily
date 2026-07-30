from __future__ import annotations

import logging
from pathlib import Path

import pytest

from scripts.build_index_data import rows_from_reports
from src.models import CachedEntry, RepoResult, TrendingRepo
from src.report import _render_index, _safe, render_report


def _repo(name: str) -> TrendingRepo:
    return TrendingRepo(
        full_name=name,
        url=f"https://github.com/{name}",
        description="專案描述",
        language="Python",
        stars_total=100,
        stars_today=10,
        rank=1,
    )


def _analysis() -> dict:
    return {
        "one_liner": "一句話摘要",
        "summary": "專案摘要",
        "category": "devtool",
        "highlights": ["主要亮點"],
        "use_cases": ["適用場景"],
        "quality": {"docs": 4, "tests": 4, "activity": 4, "comment": "品質穩定"},
        "security": {"risk_level": "none", "findings": []},
        "star_rating": 4,
        "verdict": "值得關注",
    }


def test_report_represents_every_scanned_repo(tmp_path: Path) -> None:
    fresh = RepoResult(
        repo=_repo("owner/fresh"),
        status="analyzed",
        analysis=_analysis(),
        days_on_trending=1,
        total_days_on_trending=1,
    )
    replayed = RepoResult(
        repo=_repo("owner/replayed"),
        status="analyzed",
        analysis=_analysis(),
        days_on_trending=2,
        total_days_on_trending=3,
        from_cache=True,
    )
    deferred = RepoResult(
        repo=_repo("owner/deferred"),
        status="deferred",
        error_msg="超過本次分析數量上限",
    )
    cached = CachedEntry(
        full_name="owner/cached",
        url="https://github.com/owner/cached",
        days_on_trending=1,
        total_days_on_trending=4,
        stars_today=5,
        one_liner="持續受到關注",
    )

    path = render_report(
        "2026-07-30",
        [fresh, replayed, deferred],
        [cached],
        0,
        tmp_path,
        logging.getLogger("test-report"),
        total_scanned=4,
    )
    text = path.read_text(encoding="utf-8")
    for name in ("owner/fresh", "owner/replayed", "owner/deferred", "owner/cached"):
        assert name in text
    assert 'data-scanned="4"' in text
    assert 'data-analyzed="2"' in text
    assert 'data-deferred="1"' in text
    assert "今日稍早已分析" in text
    assert "尚待分析" in text
    assert rows_from_reports(tmp_path) == [{
        "date": "2026-07-30",
        "analyzed": 2,
        "cached": 1,
        "deferred": 1,
        "top_name": "owner/fresh",
        "top_rating": 4,
    }]


def test_report_rejects_count_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="報告數量不守恆"):
        render_report(
            "2026-07-30",
            [],
            [],
            0,
            tmp_path,
            logging.getLogger("test-report"),
            total_scanned=1,
        )


def test_untrusted_markdown_link_syntax_is_neutralized() -> None:
    safe = _safe("<script>[click](https://example.test)")
    assert "<script>" not in safe
    assert "[click]" not in safe
    assert "&lt;script&gt;" in safe
    assert "&#91;click&#93;" in safe


def test_untrusted_html_is_escaped_inside_report(tmp_path: Path) -> None:
    analysis = _analysis()
    analysis["summary"] = '<img src=x onerror="alert(1)">'
    result = RepoResult(repo=_repo("owner/safe"), status="analyzed", analysis=analysis)

    path = render_report(
        "2026-07-30",
        [result],
        [],
        0,
        tmp_path,
        logging.getLogger("test-report"),
        total_scanned=1,
    )
    text = path.read_text(encoding="utf-8")
    assert '<img src=x onerror="alert(1)">' not in text
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in text


def test_homepage_uses_new_design_and_escapes_index_data() -> None:
    text = _render_index([{
        "date": "2026-07-30",
        "analyzed": 2,
        "cached": 6,
        "deferred": 1,
        "top_name": '<script>alert("x")</script>',
        "top_rating": 4,
    }], "reports")

    assert 'class="home-feature"' in text
    assert 'class="report-row"' in text
    assert 'href="reports/2026-07-30.html"' in text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
