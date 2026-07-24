"""共用工具:logging、atomic JSON 寫入、Windows 安全刪除、行程樹清理。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


def setup_logging(log_dir: Path, run_date: str, level: str = "INFO") -> logging.Logger:
    """檔案 handler(DEBUG, UTF-8)+ console handler(依設定,預設 INFO)。"""
    log_dir.mkdir(parents=True, exist_ok=True)

    # 排程環境下 stdout 可能是 cp950;能 reconfigure 就強制 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    log = logging.getLogger("bot")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fh = logging.FileHandler(log_dir / f"run-{run_date}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, level.upper(), logging.INFO))
    ch.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(ch)
    return log


def prune_old_logs(log_dir: Path, keep_days: int, log: logging.Logger) -> None:
    if not log_dir.exists():
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    for f in log_dir.glob("*.log"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                log.debug("刪除過期 log:%s", f.name)
        except OSError as e:
            log.debug("無法刪除 %s:%s", f.name, e)


def atomic_write_json(path: Path, obj) -> None:
    """先寫 .tmp 再 os.replace,避免寫到一半留下壞檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _chmod_retry(func, path, _excinfo) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree_force(path: Path, log: logging.Logger | None = None) -> bool:
    """刪除目錄樹,處理 .git 內唯讀檔與超過 260 字元的長路徑。回傳是否成功。"""
    if not path.exists():
        return True
    try:
        shutil.rmtree(path, onexc=_chmod_retry)
        return True
    except Exception as e:
        if log:
            log.debug("rmtree 失敗(%s),改用 \\\\?\\ 長路徑前綴重試:%s", e, path)
    # git core.longpaths=true 可能建出 >260 字元的路徑,一般 API 刪不掉
    try:
        shutil.rmtree("\\\\?\\" + str(path.resolve()), onexc=_chmod_retry)
        return True
    except Exception as e:
        if log:
            log.warning("長路徑 rmtree 也失敗(%s),最後手段 rmdir /s /q:%s", e, path)
    try:
        subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", str(path)],
            capture_output=True, timeout=60, check=False,
        )
    except Exception as e:
        if log:
            log.error("rmdir 也失敗:%s:%s", path, e)
    return not path.exists()


def kill_process_tree(pid: int, log: logging.Logger | None = None) -> None:
    """用 taskkill /T /F 終止整個行程樹(清掉 claude 底下的 node 子行程)。"""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=30, check=False,
        )
    except Exception as e:
        if log:
            log.warning("taskkill PID %s 失敗:%s", pid, e)
