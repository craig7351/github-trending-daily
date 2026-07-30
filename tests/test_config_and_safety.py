from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.cloner import cleanup_workspace
from src.config import load_config
from src.main import parse_args
from src.util import RunAlreadyActiveError, single_instance_lock


def _log() -> logging.Logger:
    return logging.getLogger("test-safety")


def test_workspace_must_be_strict_child_of_root(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[clone]\nworkspace_dir = "."\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="workspace_dir"):
        load_config(tmp_path)


def test_cleanup_workspace_refuses_root_and_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="拒絕清理"):
        cleanup_workspace(tmp_path, tmp_path, _log())
    with pytest.raises(ValueError, match="拒絕清理"):
        cleanup_workspace(outside, tmp_path, _log())


def test_cleanup_workspace_only_removes_child_contents(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "repo"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("x", encoding="utf-8")
    cleanup_workspace(workspace, tmp_path, _log())
    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_date_arguments_are_validated_and_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--backfill", "not-a-date"])
    with pytest.raises(SystemExit):
        parse_args([
            "--backfill", "2026-07-30",
            "--date-override", "2026-07-29",
        ])
    assert parse_args(["--date-override", "2026-07-30"]).date_override == "2026-07-30"
    with pytest.raises(SystemExit):
        parse_args(["--limit", "0"])


def test_single_instance_lock_rejects_second_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "run.lock"
    with single_instance_lock(lock_path):
        with pytest.raises(RunAlreadyActiveError):
            with single_instance_lock(lock_path):
                pass


def test_lock_file_pid_is_overwritten_not_appended(tmp_path: Path) -> None:
    """鎖檔必須只留當前 PID。append 模式會讓每輪的 PID 一直累加。"""
    import os

    lock_path = tmp_path / "run.lock"
    for _ in range(3):
        with single_instance_lock(lock_path):
            pass

    assert lock_path.read_bytes() == str(os.getpid()).encode("ascii")
