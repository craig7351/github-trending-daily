"""去重儲存:以 JSON 持久化已見過的 repo 與快取的分析結果。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from .models import TrendingRepo
from .util import atomic_write_json

_STARS_HISTORY_MAX = 60


class SeenStore:
    """seen_repos.json 的載入、更新與存檔。key 為 "owner/repo"。"""

    def __init__(self, path: Path, log: logging.Logger) -> None:
        self.path = path
        self.log = log
        self._data: dict[str, dict] = {}

    def load(self) -> None:
        if not self.path.exists():
            self.log.info("去重檔不存在,以空白狀態開始:%s", self.path)
            self._data = {}
            return
        try:
            # utf-8-sig:容忍手動編輯/PowerShell 5.1 round-trip 產生的 BOM
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise json.JSONDecodeError("頂層不是 dict", "", 0)
            self._data = data
            self.log.info("已載入去重檔:%d 筆記錄", len(self._data))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._data = {}
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            corrupt = self.path.with_name(f"{self.path.stem}.corrupt-{ts}.json")
            try:
                self.path.rename(corrupt)
                self.log.error("去重檔損毀(%s),已改名為 %s,以空白狀態重新開始", e, corrupt.name)
            except OSError as re:
                self.log.error("去重檔損毀(%s)且無法改名(%s),以空白狀態重新開始", e, re)
        except OSError as e:
            self._data = {}
            self.log.error("無法讀取去重檔 %s:%s,以空白狀態重新開始", self.path, e)

    def touch_all(self, repos: list[TrendingRepo], run_date: str) -> None:
        for repo in repos:
            entry = self._data.get(repo.full_name)
            if entry is None:
                entry = {
                    "first_seen": run_date,
                    "last_seen": run_date,
                    "days_on_trending": 1,
                    "stars_history": {},
                }
                self._data[repo.full_name] = entry
                self.log.debug("新上榜:%s", repo.full_name)
            elif entry.get("last_seen") != run_date:
                entry["days_on_trending"] = entry.get("days_on_trending", 1) + 1
                entry["last_seen"] = run_date
                self.log.debug("持續上榜:%s(第 %d 天)", repo.full_name, entry["days_on_trending"])

            history = entry.setdefault("stars_history", {})
            history[run_date] = [repo.stars_total, repo.stars_today]
            if len(history) > _STARS_HISTORY_MAX:
                # ISO 日期字串的字典序即時間序,砍掉最舊的
                for old in sorted(history)[: len(history) - _STARS_HISTORY_MAX]:
                    del history[old]
        self.log.info("去重狀態已更新:%d 個 repo,共 %d 筆記錄", len(repos), len(self._data))

    def days_on_trending(self, full_name: str) -> int:
        entry = self._data.get(full_name)
        if entry is None:
            return 1
        return entry.get("days_on_trending", 1)

    def needs_analysis(self, full_name: str, run_date: str, reanalyze_after_days: int) -> bool:
        entry = self._data.get(full_name)
        if entry is None:
            return True
        last = entry.get("last_analyzed")
        if not last:
            return True
        try:
            elapsed = (date.fromisoformat(run_date) - date.fromisoformat(last)).days
        except (ValueError, TypeError) as e:
            self.log.warning("日期解析失敗(%s,last_analyzed=%r):%s,視為需要重新分析", full_name, last, e)
            return True
        return elapsed >= reanalyze_after_days

    def analyzed_on(self, full_name: str) -> str:
        """回傳該 repo 最後一次成功分析的日期字串;沒分析過回傳空字串。"""
        entry = self._data.get(full_name)
        if entry is None:
            return ""
        return str(entry.get("last_analyzed") or "")

    def cached_analysis(self, full_name: str) -> dict | None:
        entry = self._data.get(full_name)
        if entry is None:
            return None
        return entry.get("analysis") or None

    def record_analysis(self, full_name: str, run_date: str, analysis: dict) -> None:
        entry = self._data.get(full_name)
        if entry is None:
            self.log.warning("record_analysis 找不到 %s 的記錄,自動建立", full_name)
            entry = {
                "first_seen": run_date,
                "last_seen": run_date,
                "days_on_trending": 1,
                "stars_history": {},
            }
            self._data[full_name] = entry
        entry["last_analyzed"] = run_date
        entry["analysis"] = analysis
        self.log.debug("已記錄分析結果:%s", full_name)

    def save(self) -> None:
        atomic_write_json(self.path, self._data)
        self.log.info("去重檔已存檔:%s(%d 筆記錄)", self.path, len(self._data))
