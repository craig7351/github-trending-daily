"""共用資料型別。所有模組間傳遞的資料結構都定義在這裡。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrendingRepo:
    """從 github.com/trending 頁面爬到的一個 repo。"""
    full_name: str          # "owner/repo"
    url: str                # "https://github.com/owner/repo"
    description: str        # 頁面上的描述,可能為空字串
    language: str           # 主要語言,可能為空字串
    stars_total: int
    stars_today: int        # 「N stars today」;解析不到時為 0
    rank: int               # 在 trending 頁面的順位,從 1 起算


@dataclass
class RepoMeta:
    """GitHub REST API 取得的補充 metadata。fetched=False 表示取得失敗,其餘欄位為預設值。"""
    stars: int = 0
    forks: int = 0
    license: str = ""
    topics: list[str] = field(default_factory=list)
    pushed_at: str = ""
    created_at: str = ""
    size_kb: int = 0
    open_issues: int = 0
    default_branch: str = "main"
    archived: bool = False
    description: str = ""
    language: str = ""
    fetched: bool = False


@dataclass
class RepoResult:
    """單一 repo 的處理結果,report.py 據此渲染。

    status 值:
      analyzed      — 完整 clone + claude 分析成功
      light         — 輕量分析(README-only)成功
      metadata_only — claude 失敗或被跳過,只有 metadata
      clone_failed  — clone 失敗(且無法降級成 light)
      error         — 未預期例外
    """
    repo: TrendingRepo
    meta: RepoMeta = field(default_factory=RepoMeta)
    status: str = "pending"
    analysis: dict | None = None
    error_msg: str = ""
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    days_on_trending: int = 1


@dataclass
class CachedEntry:
    """之前分析過、今天仍在榜上的 repo(報告的「持續上榜」區)。"""
    full_name: str
    url: str
    days_on_trending: int
    stars_today: int
    one_liner: str          # 取自快取分析的 one_liner,可能為空字串
