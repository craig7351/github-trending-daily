from __future__ import annotations

import json
import logging
from pathlib import Path

from src.models import TrendingRepo
from src.store import SeenStore


def _repo(name: str = "owner/repo", rank: int = 1) -> TrendingRepo:
    return TrendingRepo(
        full_name=name,
        url=f"https://github.com/{name}",
        description="description",
        language="Python",
        stars_total=100,
        stars_today=10,
        rank=rank,
    )


def _store(path: Path) -> SeenStore:
    store = SeenStore(path, logging.getLogger("test-store"))
    store.load()
    return store


def test_tracks_consecutive_and_total_days_separately(tmp_path: Path) -> None:
    store = _store(tmp_path / "seen.json")
    repo = _repo()

    store.touch_all([repo], "2026-07-25")
    store.touch_all([repo], "2026-07-27")
    assert store.days_on_trending(repo.full_name) == 1
    assert store.total_days_on_trending(repo.full_name) == 2

    store.touch_all([repo], "2026-07-28")
    store.touch_all([repo], "2026-07-28")
    assert store.days_on_trending(repo.full_name) == 2
    assert store.total_days_on_trending(repo.full_name) == 3


def test_migrates_legacy_counter_to_real_streak(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({
        "owner/repo": {
            "first_seen": "2026-07-25",
            "last_seen": "2026-07-30",
            "days_on_trending": 3,
            "stars_history": {
                "2026-07-25": [10, 2],
                "2026-07-29": [20, 3],
                "2026-07-30": [25, 5],
            },
        }
    }), encoding="utf-8")
    store = _store(path)
    assert store.days_on_trending("owner/repo") == 2
    assert store.total_days_on_trending("owner/repo") == 3


def test_daily_snapshot_can_rebuild_repo_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path / "seen.json")
    store.touch_all([_repo("owner/repo", rank=4)], "2026-07-30")
    restored = store.repos_on("2026-07-30")
    assert len(restored) == 1
    assert restored[0].full_name == "owner/repo"
    assert restored[0].rank == 4
    assert restored[0].description == "description"
    assert restored[0].language == "Python"
