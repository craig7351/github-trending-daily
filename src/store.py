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
            self._migrate_trending_counters()
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

    @staticmethod
    def _consecutive_days(history: dict, through_date: str | None = None) -> int:
        """計算截至指定日期、以最後一筆為終點的連續日數。"""
        cutoff: date | None = None
        if through_date:
            try:
                cutoff = date.fromisoformat(through_date)
            except ValueError:
                return 1

        dates: list[date] = []
        for raw in history:
            try:
                parsed = date.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            if cutoff is None or parsed <= cutoff:
                dates.append(parsed)
        if not dates:
            return 1

        dates = sorted(set(dates))
        streak = 1
        for i in range(len(dates) - 1, 0, -1):
            if (dates[i] - dates[i - 1]).days != 1:
                break
            streak += 1
        return streak

    def _migrate_trending_counters(self) -> None:
        """將舊版累計欄位遷移成「連續」與「累計」兩個計數。"""
        for entry in self._data.values():
            if not isinstance(entry, dict):
                continue
            history = entry.get("stars_history") or {}
            old_total = entry.get("total_days_on_trending", entry.get("days_on_trending", 1))
            try:
                old_total = max(1, int(old_total))
            except (TypeError, ValueError):
                old_total = max(1, len(history))
            consecutive = self._consecutive_days(history)
            entry["total_days_on_trending"] = old_total
            entry["consecutive_days_on_trending"] = consecutive
            # 保留舊欄位，避免既有資料消費者中斷；語意改為真正的連續天數。
            entry["days_on_trending"] = consecutive

    def touch_all(self, repos: list[TrendingRepo], run_date: str) -> None:
        for repo in repos:
            entry = self._data.get(repo.full_name)
            is_new = entry is None
            if is_new:
                entry = {
                    "first_seen": run_date,
                    "last_seen": run_date,
                    "days_on_trending": 1,
                    "consecutive_days_on_trending": 1,
                    "total_days_on_trending": 1,
                    "stars_history": {},
                }
                self._data[repo.full_name] = entry
                self.log.debug("新上榜:%s", repo.full_name)

            history = entry.setdefault("stars_history", {})
            already_recorded = run_date in history
            try:
                delta = (
                    date.fromisoformat(run_date) - date.fromisoformat(entry.get("last_seen"))
                ).days
            except (TypeError, ValueError):
                delta = 1

            if not is_new and not already_recorded:
                try:
                    total = int(entry.get(
                        "total_days_on_trending", entry.get("days_on_trending", 1)
                    )) + 1
                except (TypeError, ValueError):
                    total = max(1, len(history) + 1)
                entry["total_days_on_trending"] = total
            if not is_new and delta > 0:
                try:
                    previous_streak = int(entry.get("consecutive_days_on_trending", 1))
                except (TypeError, ValueError):
                    previous_streak = 1
                consecutive = previous_streak + 1 if delta == 1 else 1
                entry["consecutive_days_on_trending"] = consecutive
                entry["days_on_trending"] = consecutive
                entry["last_seen"] = run_date
                self.log.debug(
                    "持續上榜:%s(連續 %d 天、累計 %d 天)",
                    repo.full_name,
                    consecutive,
                    entry["total_days_on_trending"],
                )

            # 前兩格維持舊格式；後三格保存同日重跑合併所需的榜單快照。
            history[run_date] = [
                repo.stars_total,
                repo.stars_today,
                repo.rank,
                repo.description,
                repo.language,
            ]
            if len(history) > _STARS_HISTORY_MAX:
                # ISO 日期字串的字典序即時間序,砍掉最舊的
                for old in sorted(history)[: len(history) - _STARS_HISTORY_MAX]:
                    del history[old]
        self.log.info("去重狀態已更新:%d 個 repo,共 %d 筆記錄", len(repos), len(self._data))

    def days_on_trending(self, full_name: str) -> int:
        entry = self._data.get(full_name)
        if entry is None:
            return 1
        return int(entry.get("consecutive_days_on_trending", entry.get("days_on_trending", 1)))

    def total_days_on_trending(self, full_name: str) -> int:
        entry = self._data.get(full_name)
        if entry is None:
            return 1
        return int(entry.get("total_days_on_trending", entry.get("days_on_trending", 1)))

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

    def trending_on(self, run_date: str) -> list[tuple[str, int, int]]:
        """回傳指定日期在榜的 [(full_name, stars_total, stars_today)],供補跑歷史報告用。
        依當日新增 star 數排序,近似當天的榜單順序。"""
        rows = []
        for full_name, entry in self._data.items():
            hist = (entry.get("stars_history") or {}).get(run_date)
            if isinstance(hist, list) and len(hist) >= 2:
                rows.append((full_name, int(hist[0]), int(hist[1])))
        rows.sort(key=lambda x: -x[2])
        return rows

    def repos_on(self, run_date: str) -> list[TrendingRepo]:
        """以當日快照重建 repo，供同日重跑合併先前看過的榜單項目。"""
        repos: list[TrendingRepo] = []
        for full_name, entry in self._data.items():
            hist = (entry.get("stars_history") or {}).get(run_date)
            if not isinstance(hist, list) or len(hist) < 2:
                continue
            try:
                rank = int(hist[2]) if len(hist) > 2 else 0
            except (TypeError, ValueError):
                rank = 0
            repos.append(TrendingRepo(
                full_name=full_name,
                url=f"https://github.com/{full_name}",
                description=str(hist[3]) if len(hist) > 3 else "",
                language=str(hist[4]) if len(hist) > 4 else "",
                stars_total=int(hist[0]),
                stars_today=int(hist[1]),
                rank=rank,
            ))
        repos.sort(key=lambda r: (r.rank <= 0, r.rank if r.rank > 0 else -r.stars_today))
        return repos

    def days_on_trending_at(self, full_name: str, run_date: str) -> int:
        """該 repo 截至 run_date 的連續上榜天數。"""
        entry = self._data.get(full_name)
        if entry is None:
            return 1
        hist = entry.get("stars_history") or {}
        return self._consecutive_days(hist, through_date=run_date)

    def total_days_on_trending_at(self, full_name: str, run_date: str) -> int:
        entry = self._data.get(full_name)
        if entry is None:
            return 1
        hist = entry.get("stars_history") or {}
        return max(1, sum(1 for d in hist if d <= run_date))

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
                "consecutive_days_on_trending": 1,
                "total_days_on_trending": 1,
                "stars_history": {},
            }
            self._data[full_name] = entry
        entry["last_analyzed"] = run_date
        entry["analysis"] = analysis
        self.log.debug("已記錄分析結果:%s", full_name)

    def save(self) -> None:
        atomic_write_json(self.path, self._data)
        self.log.info("去重檔已存檔:%s(%d 筆記錄)", self.path, len(self._data))
