from __future__ import annotations

import logging

from src.trending import _parse_page


def test_parse_trending_article() -> None:
    html = """
    <article class="Box-row">
      <h2><a href="/owner/repo">owner / repo</a></h2>
      <p class="col-9 color-fg-muted">A useful project</p>
      <span itemprop="programmingLanguage">Python</span>
      <a href="/owner/repo/stargazers">1,234</a>
      <span>456 stars today</span>
    </article>
    """
    repos = _parse_page(html, "fixture", logging.getLogger("test-trending"))
    assert len(repos) == 1
    assert repos[0].full_name == "owner/repo"
    assert repos[0].stars_total == 1234
    assert repos[0].stars_today == 456
    assert repos[0].language == "Python"


def test_regex_fallback_when_box_row_class_changes() -> None:
    html = """
    <article>
      <h2><a href="/owner/repo">owner/repo</a></h2>
      <a href="/owner/repo/stargazers">99</a>
      <span>12 stars today</span>
    </article>
    """
    repos = _parse_page(html, "fixture", logging.getLogger("test-trending"))
    assert [(r.full_name, r.stars_total, r.stars_today) for r in repos] == [
        ("owner/repo", 99, 12)
    ]
