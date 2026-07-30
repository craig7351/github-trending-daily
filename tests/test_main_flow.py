from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

import src.main as main_module
from src.config import Config
from src.models import RepoMeta, TrendingRepo
from src.store import SeenStore


def _repo(name: str, rank: int) -> TrendingRepo:
    return TrendingRepo(
        full_name=name,
        url=f"https://github.com/{name}",
        description=f"{name} description",
        language="Python",
        stars_total=100,
        stars_today=10,
        rank=rank,
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
        "_mode": "full",
    }


def test_limit_keeps_deferred_and_same_day_replayed_repos(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = Config(root=tmp_path)
    cfg.scan.max_repos = 1
    cfg.report.git_commit = False
    cfg.prompts_path.mkdir()
    (cfg.prompts_path / "analysis_schema.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    (cfg.prompts_path / "repo_analysis.md").write_text("", encoding="utf-8")
    (cfg.prompts_path / "repo_analysis_light.md").write_text("", encoding="utf-8")

    old = _repo("owner/old", 1)
    store = SeenStore(cfg.store_path, logging.getLogger("test-main-store"))
    store.load()
    store.touch_all([old], "2026-07-30")
    store.record_analysis(old.full_name, "2026-07-30", _analysis())
    store.save()

    current = [_repo("owner/new-one", 1), _repo("owner/new-two", 2)]
    monkeypatch.setattr(main_module, "scrape_trending", lambda _cfg, _log: current)
    monkeypatch.setattr(main_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        main_module,
        "fetch_metadata",
        lambda *_args, **_kwargs: RepoMeta(fetched=False),
    )
    monkeypatch.setattr(main_module, "get_github_token", lambda *_args: None)

    args = argparse.Namespace(
        limit=1,
        dry_run=False,
        skip_claude=True,
        no_publish=True,
        date_override=None,
        force="",
        backfill=None,
    )
    code = main_module._run(
        args, cfg, "2026-07-30", logging.getLogger("test-main"), {}
    )

    assert code == main_module.EXIT_DEGRADED
    text = (cfg.report_path / "2026-07-30.md").read_text(encoding="utf-8")
    for name in ("owner/new-one", "owner/new-two", "owner/old"):
        assert name in text
    assert 'data-scanned="3"' in text
    assert 'data-replayed="1"' in text
    assert 'data-deferred="1"' in text
    assert "今日稍早已分析" in text
    assert "尚待分析" in text


def test_publish_commits_only_generated_paths(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(root=tmp_path)
    calls: list[list[str]] = []

    def fake_git(args, _root, _git_exe, timeout=60):
        calls.append(args)
        stdout = "origin\n" if args == ["remote"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(main_module, "_git", fake_git)
    main_module._publish(
        tmp_path, "2026-07-30", "git", cfg, logging.getLogger("test-publish")
    )

    add = calls[0]
    commit = calls[1]
    assert add[:2] == ["add", "--"]
    assert commit[:2] == ["commit", "--only"]
    assert "reports\\2026-07-30.md" in add or "reports/2026-07-30.md" in add
    assert "unrelated.txt" not in " ".join(commit)
