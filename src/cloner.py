"""淺層 clone:把 trending repo 以 shallow + blob filter 方式 clone 進 workspace,並負責清理。"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from .config import Config
from .util import rmtree_force

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def clone_dir_for(full_name: str, workspace: Path) -> Path:
    """回傳 repo 在 workspace 內的目錄路徑:owner__repo,危險字元換成底線。"""
    return workspace / _UNSAFE_RE.sub("_", full_name.replace("/", "__"))


def shallow_clone(full_name: str, dest: Path, cfg: Config, log: logging.Logger) -> bool:
    """淺層 clone 到 dest。失敗時清掉殘留目錄並回傳 False,絕不拋出例外。"""
    git = shutil.which("git")
    if not git:
        log.error("找不到 git 執行檔,無法 clone %s", full_name)
        return False

    if dest.exists():
        rmtree_force(dest, log)

    cmd = [
        git, "-c", "core.longpaths=true", "-c", "credential.helper=",
        "clone", "--depth", str(cfg.clone.depth), "--single-branch",
        f"--filter=blob:limit={cfg.clone.blob_limit}", "--no-tags", "--quiet",
        f"https://github.com/{full_name}.git", str(dest),
    ]
    # 禁止任何互動式帳密詢問,私有/不存在的 repo 直接失敗
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}

    log.debug("開始 clone:%s -> %s", full_name, dest)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=cfg.clone.timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log.warning("clone 逾時(%s 秒):%s", cfg.clone.timeout_sec, full_name)
        rmtree_force(dest, log)
        return False
    except Exception as e:
        log.warning("clone 未預期錯誤:%s:%s", full_name, e)
        rmtree_force(dest, log)
        return False

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-300:]
        log.warning("clone 失敗(exit %s):%s:%s", proc.returncode, full_name, tail)
        rmtree_force(dest, log)
        return False

    log.info("clone 完成:%s", full_name)
    return True


def remove_clone(dest: Path, log: logging.Logger) -> None:
    """刪除單一 clone 目錄。"""
    ok = rmtree_force(dest, log)
    log.debug("移除 clone %s:%s", dest, "成功" if ok else "失敗")


def cleanup_workspace(workspace: Path, log: logging.Logger) -> None:
    """確保 workspace 存在並清空其內容(保留 workspace 本身)。"""
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        children = list(workspace.iterdir())
    except OSError as e:
        log.warning("無法列舉 workspace:%s:%s", workspace, e)
        return

    for child in children:
        if child.is_dir() and not child.is_symlink():
            rmtree_force(child, log)
        else:
            try:
                child.unlink()
            except PermissionError:
                try:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
                except OSError:
                    pass
            except OSError:
                pass
        if child.exists():
            log.warning("workspace 殘留無法刪除:%s", child)

    log.debug("workspace 清理完成:%s", workspace)
