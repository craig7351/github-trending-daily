"""GitHub REST API 補充資料:repo metadata 與 README 原文擷取。
所有網路失敗都內部吞掉,回傳 fetched=False 或空字串,不往外拋例外。"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time

import requests

from .config import Config
from .models import RepoMeta

_API_BASE = "https://api.github.com/repos"
_TIMEOUT = 20
_ATTEMPTS = 2
_RETRY_SLEEP = 3


def get_github_token(cfg: Config, log: logging.Logger) -> str | None:
    """透過 gh CLI 取得 token;任何失敗都回傳 None(改用匿名額度)。"""
    if not cfg.github.use_gh_token:
        log.debug("設定停用 gh token,使用匿名 API")
        return None
    gh = shutil.which("gh")
    if not gh:
        log.debug("PATH 找不到 gh,使用匿名 API")
        return None
    try:
        proc = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True, timeout=15,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        log.debug("執行 gh auth token 失敗:%s", e)
        return None
    if proc.returncode != 0:
        log.debug("gh auth token 回傳碼 %s:%s", proc.returncode, (proc.stderr or "").strip())
        return None
    token = (proc.stdout or "").strip()
    if not token:
        log.debug("gh auth token 輸出為空")
        return None
    log.debug("已取得 gh token")
    return token


def _headers(token: str | None, user_agent: str) -> dict[str, str]:
    h = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_license(data: dict) -> str:
    lic = data.get("license")
    if not isinstance(lic, dict):
        return ""
    spdx = lic.get("spdx_id") or ""
    if spdx == "NOASSERTION":
        return lic.get("name") or ""
    return spdx


def fetch_metadata(full_name: str, token: str | None, user_agent: str,
                   log: logging.Logger) -> RepoMeta:
    """呼叫 /repos/{full_name};失敗回傳 RepoMeta(fetched=False)。"""
    url = f"{_API_BASE}/{full_name}"
    headers = _headers(token, user_agent)
    last_reason = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                meta = RepoMeta(
                    stars=data.get("stargazers_count") or 0,
                    forks=data.get("forks_count") or 0,
                    license=_parse_license(data),
                    topics=data.get("topics") or [],
                    pushed_at=data.get("pushed_at") or "",
                    created_at=data.get("created_at") or "",
                    size_kb=data.get("size") or 0,
                    open_issues=data.get("open_issues_count") or 0,
                    default_branch=data.get("default_branch") or "main",
                    archived=bool(data.get("archived")),
                    fetched=True,
                )
                log.debug("metadata 取得成功:%s(stars=%d)", full_name, meta.stars)
                return meta
            last_reason = f"HTTP {resp.status_code}"
        except Exception as e:
            last_reason = str(e)
        log.debug("metadata 第 %d 次嘗試失敗:%s:%s", attempt, full_name, last_reason)
        if attempt < _ATTEMPTS:
            time.sleep(_RETRY_SLEEP)
    log.warning("metadata 取得失敗:%s(%s)", full_name, last_reason)
    return RepoMeta(fetched=False)


def fetch_readme(full_name: str, token: str | None, user_agent: str,
                 max_chars: int, log: logging.Logger) -> str:
    """取得 README 原文並截斷至 max_chars;失敗回傳空字串。"""
    url = f"{_API_BASE}/{full_name}/readme"
    headers = _headers(token, user_agent)
    headers["Accept"] = "application/vnd.github.raw+json"
    last_reason = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                text = resp.text
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n\n[... README 已截斷 ...]"
                    log.debug("README 已截斷:%s(%d 字元上限)", full_name, max_chars)
                return text
            last_reason = f"HTTP {resp.status_code}"
        except Exception as e:
            last_reason = str(e)
        log.debug("README 第 %d 次嘗試失敗:%s:%s", attempt, full_name, last_reason)
        if attempt < _ATTEMPTS:
            time.sleep(_RETRY_SLEEP)
    log.warning("README 取得失敗:%s(%s)", full_name, last_reason)
    return ""
