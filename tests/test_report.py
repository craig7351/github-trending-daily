from __future__ import annotations

import logging
from pathlib import Path

import pytest

from scripts.build_index_data import rows_from_reports
from src.models import CachedEntry, RepoResult, TrendingRepo
from src.report import _safe, render_report


def _repo(name: str) -> TrendingRepo:
    return TrendingRepo(
        full_name=name,
        url=f"https://github.com/{name}",
        description="description",
        language="Python",
        stars_total=100,
        stars_today=10,
        rank=1,
    )


def _analysis() -> dict:
    return {
        "one_liner": "一句話",
        "summary": "摘要",
        "category": "devtool",
        "highlights": ["亮點"],
        "use_cases": ["用途"],
        "quality": {"docs": 4, "tests": 4, "activity": 4, "comment": "品質良好"},
        "security": {"risk_level": "none", "findings": []},
        "star_rating": 4,
        "verdict": "值得關注",
    }


def test_report_represents_every_scanned_repo(tmp_path: Path) -> None:
    fresh = RepoResult(
        repo=_repo("owner/fresh"), status="analyzed", analysis=_analysis(),
        days_on_trending=1, total_days_on_trending=1,
    )
    replayed = RepoResult(
        repo=_repo("owner/replayed"), status="analyzed", analysis=_analysis(),
        days_on_trending=2, total_days_on_trending=3, from_cache=True,
    )
    deferred = RepoResult(
        repo=_repo("owner/deferred"), status="deferred",
        error_msg="超過本次分析數量上限",
    )
    cached = CachedEntry(
        full_name="owner/cached",
        url="https://github.com/owner/cached",
        days_on_trending=1,
        total_days_on_trending=4,
        stars_today=5,
        one_liner="快取",
    )

    path = render_report(
        "2026-07-30", [fresh, replayed, deferred], [cached], 0,
        tmp_path, logging.getLogger("test-report"), total_scanned=4,
    )
    text = path.read_text(encoding="utf-8")
    for name in ("owner/fresh", "owner/replayed", "owner/deferred", "owner/cached"):
        assert name in text
    assert "- 待分析 1 個" in text
    assert "## ♻️ 今日稍早已分析" in text
    assert "## ⏳ 尚待分析" in text
    assert rows_from_reports(tmp_path) == [{
        "date": "2026-07-30",
        "analyzed": 2,
        "cached": 1,
        "deferred": 1,
        "top_name": "owner/fresh",
        "top_rating": 4,
    }]


def test_report_rejects_count_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="數量不守恆"):
        render_report(
            "2026-07-30", [], [], 0, tmp_path,
            logging.getLogger("test-report"), total_scanned=1,
        )


def test_untrusted_markdown_link_syntax_is_neutralized() -> None:
    safe = _safe("<script>[click](https://example.test)")
    assert "<script>" not in safe
    assert "[click]" not in safe
    assert "&lt;script&gt;" in safe
    assert "&#91;click&#93;" in safe
