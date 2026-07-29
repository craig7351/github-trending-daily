"""設定載入:config.toml + 預設值。tomllib 為 Python 3.11+ 標準庫。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScanCfg:
    max_repos: int = 10
    trending_since: str = "daily"
    extra_language_pages: list[str] = field(default_factory=list)
    user_agent: str = "github-star-bot/1.0"


@dataclass
class CloneCfg:
    depth: int = 1
    blob_limit: str = "200k"
    timeout_sec: int = 180
    max_repo_mb: int = 300
    workspace_dir: str = "workspace"


@dataclass
class AnalysisCfg:
    model: str = "sonnet"
    timeout_sec: int = 420
    max_budget_usd: float = 1.5
    consecutive_failure_stop: int = 3
    readme_max_chars: int = 12000
    claude_path: str = ""
    light_patterns: list[str] = field(default_factory=lambda: [
        "awesome", "roadmap", "interview", "tutorial",
        "cheatsheet", "book", "course", "curated", "list",
    ])


@dataclass
class DedupCfg:
    reanalyze_after_days: int = 14
    store_path: str = "data/seen_repos.json"


@dataclass
class GithubCfg:
    use_gh_token: bool = True


@dataclass
class ReportCfg:
    dir: str = "reports"
    git_commit: bool = False
    git_push: bool = False


@dataclass
class LoggingCfg:
    dir: str = "logs"
    level: str = "INFO"
    keep_days: int = 30


@dataclass
class Config:
    root: Path
    scan: ScanCfg = field(default_factory=ScanCfg)
    clone: CloneCfg = field(default_factory=CloneCfg)
    analysis: AnalysisCfg = field(default_factory=AnalysisCfg)
    dedup: DedupCfg = field(default_factory=DedupCfg)
    github: GithubCfg = field(default_factory=GithubCfg)
    report: ReportCfg = field(default_factory=ReportCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)

    # 由 root 推導的絕對路徑
    @property
    def workspace_path(self) -> Path:
        return self.root / self.clone.workspace_dir

    @property
    def store_path(self) -> Path:
        return self.root / self.dedup.store_path

    @property
    def report_path(self) -> Path:
        return self.root / self.report.dir

    @property
    def log_path(self) -> Path:
        return self.root / self.logging.dir

    @property
    def prompts_path(self) -> Path:
        return self.root / "prompts"


_SECTION_CLASSES = {
    "scan": ScanCfg,
    "clone": CloneCfg,
    "analysis": AnalysisCfg,
    "dedup": DedupCfg,
    "github": GithubCfg,
    "report": ReportCfg,
    "logging": LoggingCfg,
}


def load_config(root: Path) -> Config:
    """讀取 root/config.toml;檔案或欄位缺失時使用預設值,未知欄位忽略並不報錯。"""
    raw: dict = {}
    toml_file = root / "config.toml"
    if toml_file.exists():
        with open(toml_file, "rb") as f:
            raw = tomllib.load(f)

    cfg = Config(root=root)
    for section, cls in _SECTION_CLASSES.items():
        data = raw.get(section, {})
        if not isinstance(data, dict):
            continue
        obj = getattr(cfg, section)
        for key, value in data.items():
            if hasattr(obj, key):
                setattr(obj, key, value)
    return cfg
