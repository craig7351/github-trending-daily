"""CLI 入口與每日掃描編排:trending → 去重 → metadata → clone → AI 分析 → 報告。"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

from .analyzer import build_prompt, is_systemic_error, resolve_claude, run_claude_analysis
from .cloner import cleanup_workspace, clone_dir_for, remove_clone, shallow_clone
from .config import Config, load_config
from .github_meta import fetch_metadata, fetch_readme, get_github_token
from .models import CachedEntry, RepoMeta, RepoResult, TrendingRepo
from .report import INDEX_DATA_REL, render_report, render_stub_report, update_index
from .store import SeenStore
from .trending import TrendingFetchError, scrape_trending
from .util import (
    RunAlreadyActiveError,
    configure_utf8_stdio,
    prune_old_logs,
    setup_logging,
    single_instance_lock,
)

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FATAL = 2


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise argparse.ArgumentTypeError("日期必須是有效的 YYYY-MM-DD") from e


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("數量上限必須是正整數") from e
    if parsed <= 0:
        raise argparse.ArgumentTypeError("數量上限必須是正整數")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="github-star", description="GitHub Trending 每日掃描機器人")
    p.add_argument("--limit", type=_positive_limit, default=0, help="覆寫 config 的 max_repos")
    p.add_argument("--dry-run", action="store_true", help="只抓 trending 並印出選擇結果,不 clone、不分析、不寫檔")
    p.add_argument("--skip-claude", action="store_true", help="跳過 AI 分析(驗證 clone 與清理用)")
    p.add_argument("--no-publish", action="store_true",
                   help="仍產生本機報告，但不 git commit/push")
    p.add_argument("--force", default="", metavar="OWNER/REPO", help="強制重新分析指定 repo(須在今日榜上,不受數量上限限制)")
    dates = p.add_mutually_exclusive_group()
    dates.add_argument("--date-override", default=None, type=_iso_date, metavar="YYYY-MM-DD",
                       help="覆寫執行日期(測試去重用)")
    dates.add_argument("--backfill", default=None, type=_iso_date, metavar="YYYY-MM-DD",
                       help="補跑歷史報告:榜單改由去重檔的 stars_history 重建,不抓 trending、不更新上榜天數")
    return p.parse_args(argv)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def classify_light(repo: TrendingRepo, meta: RepoMeta, cfg: Config) -> tuple[bool, str]:
    """判斷是否走輕量分析(不 clone)。回傳 (是否輕量, 原因)。"""
    name_part = repo.full_name.split("/")[-1].lower()
    desc = (repo.description or "").lower()
    for pat in cfg.analysis.light_patterns:
        # 詞邊界比對:「book」不可命中「notebook」、「course」不可命中「discourse」
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(pat.lower()) + r"(?![a-z0-9])")
        if rx.search(name_part) or rx.search(desc):
            return True, f"名稱/描述符合輕量規則「{pat}」"
    if meta.fetched and meta.size_kb > cfg.clone.max_repo_mb * 1024:
        return True, f"repo 過大({meta.size_kb // 1024} MB > {cfg.clone.max_repo_mb} MB)"
    return False, ""


def run(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        cfg = load_config(root)
    except Exception as e:
        print(f"config.toml 讀取失敗:{e}", file=sys.stderr)
        return EXIT_FATAL
    if args.limit > 0:
        cfg.scan.max_repos = args.limit

    run_date = args.backfill or args.date_override or datetime.now().strftime("%Y-%m-%d")
    log = setup_logging(cfg.log_path, run_date, cfg.logging.level)
    prune_old_logs(cfg.log_path, cfg.logging.keep_days, log)

    # 頂層防護:任何未預期錯誤 → 搶救 store、補 stub 報告、exit 2(與「降級」的 exit 1 區分)
    state: dict = {}
    try:
        with single_instance_lock(root / ".github-star.lock"):
            return _run(args, cfg, run_date, log, state)
    except RunAlreadyActiveError as e:
        log.error("%s", e)
        return EXIT_FATAL
    except Exception:
        log.critical("未預期的致命錯誤:\n%s", traceback.format_exc())
        store = state.get("store")
        if store is not None:
            try:
                store.save()
                log.info("已搶救存檔 store")
            except Exception as e:
                log.error("搶救 store 失敗:%s", e)
        report_file = cfg.report_path / f"{run_date}.md"
        if not report_file.exists():
            try:
                render_stub_report(run_date, "執行過程發生未預期錯誤,詳見當日 log", cfg.report_path, log)
            except Exception as e:
                log.error("寫入替代報告失敗:%s", e)
        return EXIT_FATAL


def _run(args: argparse.Namespace, cfg: Config, run_date: str,
         log, state: dict) -> int:
    log.info("=== GitHub Trending 掃描開始(%s)===", run_date)
    log.info("設定:max_repos=%d dry_run=%s skip_claude=%s no_publish=%s",
             cfg.scan.max_repos, args.dry_run, args.skip_claude, args.no_publish)

    # --- preflight:工具解析(失敗走降級,不中斷) ---
    git_exe = shutil.which("git")
    claude_exe = None if (args.skip_claude or args.dry_run) else resolve_claude(cfg, log)
    log.info("preflight:git=%s claude=%s", git_exe or "(找不到)", claude_exe or "(不使用)")

    store = SeenStore(cfg.store_path, log)
    store.load()
    state["store"] = store

    if args.backfill:
        # --- 補跑:榜單由歷史重建,不抓 trending、不 touch(否則會污染上榜天數) ---
        rows = store.trending_on(run_date)
        if not rows:
            log.critical("去重檔中沒有 %s 的榜單記錄,無法補跑", run_date)
            return EXIT_FATAL
        repos = [
            TrendingRepo(full_name=name, url=f"https://github.com/{name}",
                         description="", language="",
                         stars_total=total, stars_today=today, rank=i)
            for i, (name, total, today) in enumerate(rows, start=1)
        ]
        log.info("補跑 %s:由歷史重建 %d 個上榜專案", run_date, len(repos))
    else:
        # --- 抓 trending(致命路徑;不覆寫同日既有的正常報告) ---
        try:
            repos = scrape_trending(cfg, log)
        except TrendingFetchError as e:
            log.critical("trending 抓取失敗:%s", e)
            if not args.dry_run:
                report_file = cfg.report_path / f"{run_date}.md"
                if report_file.exists():
                    log.warning("今日報告已存在,保留不覆寫:%s", report_file)
                else:
                    render_stub_report(run_date, str(e), cfg.report_path, log)
                    update_index(run_date, 0, 0, None, cfg.root, log, cfg.report.dir)
            return EXIT_FATAL
        log.info("抓到 %d 個上榜專案", len(repos))
        store.touch_all(repos, run_date)
        # 同日重跑採聯集語意：保留今天稍早看過、但此刻已掉出榜單的項目，
        # 避免小範圍測試或榜單變動把既有日報縮水。
        current_names = {r.full_name for r in repos}
        restored = 0
        for prior in store.repos_on(run_date):
            if prior.full_name in current_names:
                continue
            prior.rank = len(repos) + 1
            repos.append(prior)
            current_names.add(prior.full_name)
            restored += 1
        if restored:
            log.info("同日重跑:從當日快照補回 %d 個先前上榜專案", restored)

    def days_of(name: str) -> int:
        return (store.days_on_trending_at(name, run_date) if args.backfill
                else store.days_on_trending(name))

    def total_days_of(name: str) -> int:
        return (store.total_days_on_trending_at(name, run_date) if args.backfill
                else store.total_days_on_trending(name))

    selected: list[TrendingRepo] = []
    cached: list[CachedEntry] = []
    replayed: list[RepoResult] = []
    deferred: list[RepoResult] = []
    for r in repos:
        prev = store.cached_analysis(r.full_name)
        if args.backfill:
            # 補跑:有快取就完整重現,沒有的才分析(上限內)
            if prev:
                replayed.append(RepoResult(
                    repo=r,
                    status="light" if prev.get("_mode") == "light" else "analyzed",
                    analysis=prev,
                    days_on_trending=days_of(r.full_name),
                    total_days_on_trending=total_days_of(r.full_name),
                    from_cache=True,
                ))
            elif len(selected) < cfg.scan.max_repos:
                selected.append(r)
            else:
                deferred.append(RepoResult(
                    repo=r,
                    status="deferred",
                    error_msg="超過本次分析數量上限",
                    days_on_trending=days_of(r.full_name),
                    total_days_on_trending=total_days_of(r.full_name),
                ))
            continue

        force_this = bool(args.force) and r.full_name.lower() == args.force.lower()
        needs = store.needs_analysis(r.full_name, run_date, cfg.dedup.reanalyze_after_days)
        if force_this or (needs and len(selected) < cfg.scan.max_repos):
            selected.append(r)
            continue
        if prev and store.analyzed_on(r.full_name) == run_date:
            # 同日重跑:以快取完整重現分析區塊,報告內容不因重跑而降級
            replayed.append(RepoResult(
                repo=r,
                status="light" if prev.get("_mode") == "light" else "analyzed",
                analysis=prev,
                days_on_trending=days_of(r.full_name),
                total_days_on_trending=total_days_of(r.full_name),
                from_cache=True,
            ))
        elif needs:
            deferred.append(RepoResult(
                repo=r,
                status="deferred",
                error_msg="超過本次分析數量上限",
                days_on_trending=days_of(r.full_name),
                total_days_on_trending=total_days_of(r.full_name),
            ))
        elif prev:
            cached.append(CachedEntry(
                full_name=r.full_name, url=r.url,
                days_on_trending=days_of(r.full_name),
                total_days_on_trending=total_days_of(r.full_name),
                stars_today=r.stars_today,
                one_liner=str(prev.get("one_liner", "")),
            ))
    if args.force and not any(x.full_name.lower() == args.force.lower() for x in repos):
        log.warning("--force 指定的 %s 不在今日榜上,已忽略", args.force)
    log.info("本日分析 %d 個、同日快取重現 %d 個、持續上榜 %d 個、待分析 %d 個",
             len(selected), len(replayed), len(cached), len(deferred))

    if args.dry_run:
        log.info("--dry-run 選擇結果:")
        for r in selected:
            log.info("  [分析] #%-2d %-45s ⭐%-8d +%d", r.rank, r.full_name, r.stars_total, r.stars_today)
        for x in replayed:
            log.info("  [同日重現] %-43s ★%s", x.repo.full_name, _safe_int((x.analysis or {}).get("star_rating")))
        for c in cached:
            log.info("  [快取] %-48s 第 %d 天", c.full_name, c.days_on_trending)
        for r in deferred:
            log.info("  [待分析] %-44s 超過本次上限", r.repo.full_name)
        return EXIT_OK

    # --- 分析資源準備 ---
    token = get_github_token(cfg, log)
    schema_str = (cfg.prompts_path / "analysis_schema.json").read_text(encoding="utf-8")
    full_template = (cfg.prompts_path / "repo_analysis.md").read_text(encoding="utf-8")
    light_template = (cfg.prompts_path / "repo_analysis_light.md").read_text(encoding="utf-8")
    cleanup_workspace(cfg.workspace_path, cfg.root, log)
    empty_dir = cfg.workspace_path / "_empty"   # 輕量分析的工作目錄(空,避免存取專案檔案)
    empty_dir.mkdir(parents=True, exist_ok=True)

    fresh: list[RepoResult] = []
    total_cost = 0.0
    record_date = datetime.now().strftime("%Y-%m-%d") if args.backfill else run_date
    consecutive_systemic = 0    # 認證/額度類:整輪都會失敗,提早停止
    consecutive_any = 0         # 任何失敗:高門檻安全網,避免未知故障空轉整輪
    claude_dead = claude_exe is None
    any_stop = max(cfg.analysis.consecutive_failure_stop * 2, 6)

    # --- 逐一分析(單 repo 隔離) ---
    for r in selected:
        t0 = time.monotonic()
        res = RepoResult(repo=r)
        try:
            res.meta = fetch_metadata(r.full_name, token, cfg.scan.user_agent, log)
            res.days_on_trending = days_of(r.full_name)
            res.total_days_on_trending = total_days_of(r.full_name)
            if not r.description:
                r.description = res.meta.description   # 補跑時爬蟲資料不存在,用 API 的
            if not r.language:
                r.language = res.meta.language
            light, light_reason = classify_light(r, res.meta, cfg)
            log.info("[%d/%d] %s(%s)", len(fresh) + 1, len(selected), r.full_name,
                     f"輕量:{light_reason}" if light else "完整分析")

            if claude_dead:
                res.status = "metadata_only"
                res.error_msg = ("已跳過 AI 分析(--skip-claude)" if args.skip_claude
                                 else "AI 分析已停用(工具缺失或連續失敗)")
                if args.skip_claude and not light and git_exe:
                    # --skip-claude 模式仍演練 clone + 清理
                    dest = clone_dir_for(r.full_name, cfg.workspace_path)
                    ok = shallow_clone(r.full_name, dest, cfg, log)
                    remove_clone(dest, log)
                    if not ok:
                        res.status = "clone_failed"
                        res.error_msg = "clone 失敗"
                fresh.append(res)
                continue

            values: dict[str, object] = {
                "full_name": r.full_name, "rank": r.rank,
                "stars_total": r.stars_total, "stars_today": r.stars_today,
                "days_on_trending": res.days_on_trending,
                "description": r.description or "(無)",
                "language": r.language or "(未標示)",
                "topics": ", ".join(res.meta.topics) or "(無)",
                "license": res.meta.license or "(未標示)",
                "pushed_at": res.meta.pushed_at or "(未知)",
            }

            # cwd 固定在自家空白目錄;不可信 repo 只透過 --add-dir 授權唯讀
            clone_dest: Path | None = None
            add_dir: Path | None = None
            if light:
                readme = fetch_readme(r.full_name, token, cfg.scan.user_agent, cfg.analysis.readme_max_chars, log)
                values["readme_content"] = readme or "(無法取得 README)"
                prompt = build_prompt(light_template, values)
            else:
                clone_dest = clone_dir_for(r.full_name, cfg.workspace_path)
                if git_exe and shallow_clone(r.full_name, clone_dest, cfg, log):
                    values["repo_path"] = str(clone_dest.resolve())
                    prompt = build_prompt(full_template, values)
                    add_dir = clone_dest
                else:
                    # clone 失敗 → 降級成輕量分析
                    log.warning("%s clone 失敗,降級輕量分析", r.full_name)
                    readme = fetch_readme(r.full_name, token, cfg.scan.user_agent, cfg.analysis.readme_max_chars, log)
                    if not readme:
                        res.status = "clone_failed"
                        res.error_msg = "clone 失敗且無法取得 README"
                        fresh.append(res)
                        continue
                    light = True
                    values["readme_content"] = readme
                    prompt = build_prompt(light_template, values)

            try:
                analysis, cost, err = run_claude_analysis(
                    prompt, empty_dir, claude_exe, schema_str, cfg, log, add_dir=add_dir)
            finally:
                if clone_dest is not None:
                    remove_clone(clone_dest, log)

            total_cost += cost
            if analysis is not None:
                res.status = "light" if light else "analyzed"
                analysis["_mode"] = "light" if light else "full"
                res.analysis = analysis
                res.cost_usd = cost
                # 補跑時分析的是 repo 的「現況」,last_analyzed 記今天才誠實
                store.record_analysis(r.full_name, record_date, analysis)
                store.save()   # 增量存檔:中途被殺(排程 2 小時上限等)也不丟已花錢的結果
                consecutive_systemic = 0
                consecutive_any = 0
                log.info("  ✓ %s ★%s($%.3f)", r.full_name, analysis.get("star_rating", "?"), cost)
            else:
                res.status = "metadata_only"
                res.error_msg = err
                consecutive_any += 1
                systemic = is_systemic_error(err)
                consecutive_systemic = consecutive_systemic + 1 if systemic else 0
                log.warning("  ✗ %s 分析失敗:%s(%s,連續 %d)", r.full_name, err,
                            "系統性" if systemic else "單一 repo", consecutive_any)
                # 只有認證/額度這類整輪性錯誤才提早停止;內容類失敗是 repo 個案,
                # 讓後面的 repo 繼續嘗試(高門檻安全網另外擋未知的全面故障)
                if consecutive_systemic >= cfg.analysis.consecutive_failure_stop:
                    claude_dead = True
                    log.error("連續 %d 次系統性失敗(額度或認證),本輪停用 AI 分析", consecutive_systemic)
                elif consecutive_any >= any_stop:
                    claude_dead = True
                    log.error("連續 %d 次失敗,判定 CLI 異常,本輪停用 AI 分析", consecutive_any)
        except Exception:
            res.status = "error"
            res.error_msg = traceback.format_exc(limit=3)
            log.error("%s 未預期錯誤:\n%s", r.full_name, res.error_msg)
            if not claude_dead:
                consecutive_any += 1
                if consecutive_any >= any_stop:
                    claude_dead = True
                    log.error("連續 %d 次失敗(含未預期錯誤),本輪停用 AI 分析", consecutive_any)
        finally:
            res.duration_sec = time.monotonic() - t0
            if res not in fresh:
                fresh.append(res)

    # --- 報告與收尾 ---
    results = fresh + replayed + deferred
    report_file = render_report(run_date, results, cached, total_cost,
                                cfg.report_path, log, total_scanned=len(repos),
                                backfilled_on=record_date if args.backfill else "")
    ok_results = [x for x in results if x.status in ("analyzed", "light") and x.analysis]
    best = max(ok_results, key=lambda x: _safe_int(x.analysis.get("star_rating")), default=None)
    top = ((best.repo.full_name, _safe_int(best.analysis.get("star_rating")))
           if best else None)
    update_index(
        run_date, len(ok_results), len(cached),
        top, cfg.root, log, cfg.report.dir, deferred_count=len(deferred),
    )
    store.save()
    cleanup_workspace(cfg.workspace_path, cfg.root, log)

    root = cfg.root
    if args.no_publish:
        log.info("--no-publish:略過 git commit/push")
    elif cfg.report.git_commit and (root / ".git").exists() and git_exe:
        _publish(root, run_date, git_exe, cfg, log)

    degraded = [x for x in fresh if x.status in ("metadata_only", "clone_failed", "error")]
    log.info("=== 完成:報告 %s|成功 %d、降級 %d、快取 %d|成本 $%.3f ===",
             report_file.name, len(ok_results), len(degraded), len(cached), total_cost)
    return EXIT_DEGRADED if degraded else EXIT_OK


def _git(args: list[str], root: Path, git_exe: str, timeout: int = 60):
    return subprocess.run([git_exe, *args], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, check=False)


def _publish(root: Path, run_date: str, git_exe: str, cfg: Config, log) -> None:
    """commit 當日產出,並(若設定且有 remote)push 上去讓 GitHub Pages 更新。

    發布失敗只記警告 — 報告已寫入本機,不該讓推送問題影響整輪的結束狀態。"""
    try:
        generated_paths = [
            str(Path(cfg.report.dir) / f"{run_date}.md"),
            cfg.dedup.store_path,
            INDEX_DATA_REL,
            "index.md",
        ]
        add = _git(["add", "--", *generated_paths], root, git_exe)
        if add.returncode != 0:
            out = ((add.stdout or "") + (add.stderr or "")).strip()
            log.warning("git add 失敗(exit %d):%s", add.returncode, out[-300:])
            return
        # --only 限定這次自動產物，避免把使用者原本 staged 的其他變更一起提交。
        commit = _git(
            [
                "commit", "--only", "-m", f"report: {run_date}", "--no-gpg-sign",
                "--", *generated_paths,
            ],
            root, git_exe,
        )
        out = ((commit.stdout or "") + (commit.stderr or "")).strip()
        if commit.returncode == 0:
            log.info("報告已 git commit")
        elif "nothing to commit" in out or "nothing added" in out:
            log.info("git commit:沒有新變更,略過 push")
            return
        else:
            log.warning("git commit 失敗(exit %d):%s", commit.returncode, out[-300:])
            return

        if not cfg.report.git_push:
            log.info("git_push 未開啟,僅本機 commit")
            return
        if not (_git(["remote"], root, git_exe).stdout or "").strip():
            log.warning("尚未設定 git remote,略過 push")
            return

        push = _git(["push"], root, git_exe, timeout=180)
        if push.returncode == 0:
            log.info("已 push 到 remote,GitHub Pages 將自動更新")
        else:
            tail = ((push.stdout or "") + (push.stderr or "")).strip()[-300:]
            log.warning("git push 失敗(exit %d):%s", push.returncode, tail)
    except subprocess.TimeoutExpired as e:
        log.warning("git 操作逾時:%s", e)
    except Exception as e:
        log.warning("發布流程發生錯誤(報告已存於本機):%s", e)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
