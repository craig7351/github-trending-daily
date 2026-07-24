"""爬取並解析 github.com/trending 頁面,產出 TrendingRepo 清單。"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from .config import Config
from .models import TrendingRepo


class TrendingFetchError(Exception):
    """trending 主頁面完全抓不到或解析為 0 個 repo 時拋出。"""


_RETRY_SLEEPS = (2, 8)  # 第 1、2 次失敗後的等待秒數,共 3 次嘗試
_STARS_PERIOD_RE = re.compile(
    r"([\d,]+)\s+stars?\s+(?:today|this\s+week|this\s+month)", re.IGNORECASE
)
_FULL_NAME_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def _fetch_page(url: str, cfg: Config, log: logging.Logger) -> str | None:
    """抓取單一頁面,最多 3 次嘗試;全部失敗回傳 None。"""
    headers = {"User-Agent": cfg.scan.user_agent, "Accept-Language": "en"}
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                log.debug("抓取成功(第 %d 次):%s", attempt, url)
                return resp.text
            log.warning("HTTP %d(第 %d 次):%s", resp.status_code, attempt, url)
        except requests.RequestException as e:
            log.warning("請求失敗(第 %d 次):%s:%s", attempt, url, e)
        if attempt < 3:
            time.sleep(_RETRY_SLEEPS[attempt - 1])
    return None


def _normalize_full_name(raw: str) -> str:
    """href "/owner/repo" -> "owner/repo";去除頁面文字可能夾帶的空白與換行。"""
    name = unquote(raw).strip().strip("/")
    return re.sub(r"\s+", "", name)


def _parse_int(text: str) -> int:
    m = re.search(r"[\d,]+", text)
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return 0


def _parse_article(art) -> TrendingRepo | None:
    h2 = art.find("h2")
    a = h2.find("a", href=True) if h2 else None
    if a is None:
        return None
    full_name = _normalize_full_name(a["href"])
    if not _FULL_NAME_RE.match(full_name):
        return None

    p = art.select_one('p[class*="col-9"]')
    description = p.get_text(strip=True) if p else ""

    lang_span = art.find("span", itemprop="programmingLanguage")
    language = lang_span.get_text(strip=True) if lang_span else ""

    star_a = art.find("a", href=lambda h: h is not None and h.endswith("/stargazers"))
    stars_total = _parse_int(star_a.get_text()) if star_a else 0

    stars_today = 0
    for span in art.find_all("span"):
        m = _STARS_PERIOD_RE.search(" ".join(span.get_text().split()))
        if m:
            stars_today = int(m.group(1).replace(",", ""))
            break

    return TrendingRepo(
        full_name=full_name,
        url="https://github.com/" + full_name,
        description=description,
        language=language,
        stars_total=stars_total,
        stars_today=stars_today,
        rank=0,
    )


def _parse_fallback(html: str, log: logging.Logger) -> list[TrendingRepo]:
    """soup 找不到 Box-row 時的退路:直接 regex 掃 <article> 區塊。"""
    repos: list[TrendingRepo] = []
    for block in html.split("<article")[1:]:
        m = re.search(r'<h2[^>]*>.*?href="(/[^"?#]+)"', block, re.DOTALL)
        if not m:
            continue
        full_name = _normalize_full_name(m.group(1))
        if not _FULL_NAME_RE.match(full_name):
            continue

        stars_total = 0
        ms = re.search(r'href="[^"]*/stargazers"[^>]*>(.*?)</a>', block, re.DOTALL)
        if ms:
            stars_total = _parse_int(re.sub(r"<[^>]+>", " ", ms.group(1)))

        stars_today = 0
        mt = _STARS_PERIOD_RE.search(re.sub(r"<[^>]+>", " ", block))
        if mt:
            stars_today = int(mt.group(1).replace(",", ""))

        repos.append(TrendingRepo(
            full_name=full_name,
            url="https://github.com/" + full_name,
            description="",
            language="",
            stars_total=stars_total,
            stars_today=stars_today,
            rank=0,
        ))
    log.debug("fallback 解析取得 %d 個 repo", len(repos))
    return repos


def _parse_page(html: str, label: str, log: logging.Logger) -> list[TrendingRepo]:
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.find_all("article", class_="Box-row")
    if not articles:
        log.warning("%s 找不到 Box-row article,改用 regex fallback 解析", label)
        return _parse_fallback(html, log)

    repos: list[TrendingRepo] = []
    for art in articles:
        try:
            repo = _parse_article(art)
        except Exception as e:
            log.debug("%s 單筆解析失敗,略過:%s", label, e)
            continue
        if repo is not None:
            repos.append(repo)
    log.debug("%s 解析出 %d 個 repo", label, len(repos))
    return repos


def scrape_trending(cfg: Config, log: logging.Logger) -> list[TrendingRepo]:
    """抓取 trending 主頁與額外語言頁,合併去重後回傳(rank 依合併順序 1..n)。

    主頁面抓取失敗或解析為 0 個 repo 時拋出 TrendingFetchError;
    額外語言頁失敗只記 warning,不中斷。
    """
    since = cfg.scan.trending_since
    main_url = f"https://github.com/trending?since={since}"

    log.info("開始抓取 trending 主頁面(since=%s)", since)
    html = _fetch_page(main_url, cfg, log)
    if html is None:
        raise TrendingFetchError(f"trending 主頁面重試 3 次皆失敗:{main_url}")

    all_repos = _parse_page(html, "主頁面", log)
    if not all_repos:
        raise TrendingFetchError("trending 主頁面解析為 0 個 repo,頁面結構可能已變更")

    for lang in cfg.scan.extra_language_pages:
        lang_url = f"https://github.com/trending/{lang}?since={since}"
        log.info("抓取語言頁:%s", lang)
        lang_html = _fetch_page(lang_url, cfg, log)
        if lang_html is None:
            log.warning("語言頁 %s 重試皆失敗,跳過", lang)
            continue
        lang_repos = _parse_page(lang_html, f"語言頁 {lang}", log)
        if not lang_repos:
            log.warning("語言頁 %s 解析為 0 個 repo,跳過", lang)
            continue
        all_repos.extend(lang_repos)

    merged: dict[str, TrendingRepo] = {}
    for repo in all_repos:
        if repo.full_name not in merged:
            merged[repo.full_name] = repo
    result = list(merged.values())
    for i, repo in enumerate(result, start=1):
        repo.rank = i

    log.info("trending 掃描完成:合併去重後共 %d 個 repo", len(result))
    return result
