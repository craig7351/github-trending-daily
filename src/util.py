"""共用工具:logging、atomic JSON 寫入、Windows 安全刪除、行程樹清理。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator


def configure_utf8_stdio() -> None:
    """讓 Windows 直接執行 CLI（包含 --help）時也固定輸出 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(log_dir: Path, run_date: str, level: str = "INFO") -> logging.Logger:
    """檔案 handler(DEBUG, UTF-8)+ console handler(依設定,預設 INFO)。"""
    log_dir.mkdir(parents=True, exist_ok=True)

    # 排程環境下 stdout 可能是 cp950;能 reconfigure 就強制 UTF-8
    configure_utf8_stdio()

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


class RunAlreadyActiveError(RuntimeError):
    """同一份專案已有另一個掃描程序持有執行鎖。"""


@contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    """取得跨平台非阻塞檔案鎖；行程結束時由 OS 自動釋放。

    鎖檔本身會保留，只有鎖定狀態代表是否正在執行，避免 crash 後留下
    無法判斷真假的 stale PID file。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 一律用原生 fd 操作,不包 buffered IO:msvcrt.locking 鎖的是「當前檔案位置」
    # 的那一個位元組,而緩衝物件的 seek 不保證同步底層 fd 的位置 —— 那會讓上鎖與
    # 解鎖落在不同位元組,解鎖時拋 PermissionError。os.lseek 沒有這個歧義。
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

    def _rewind() -> None:
        os.lseek(fd, 0, os.SEEK_SET)

    def _busy(exc: OSError) -> RunAlreadyActiveError:
        return RunAlreadyActiveError(f"已有另一個掃描程序正在執行（鎖檔：{path}）")

    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")   # 鎖定第 0 個位元組,先確保它存在
        _rewind()
    except OSError as e:
        os.close(fd)
        raise _busy(e) from e

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        raise _busy(e) from e

    try:
        # 只留當前 PID 作為診斷資訊(覆寫而非累加)
        pid = str(os.getpid()).encode("ascii")
        _rewind()
        os.write(fd, pid)
        os.truncate(fd, len(pid))
        yield
    finally:
        try:
            _rewind()
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
