# GitHub Trending 每日掃描機器人

每天自動掃描 [GitHub Trending](https://github.com/trending),對熱門專案做 **AI 靜態分析**(絕不執行專案程式碼),產出繁體中文 Markdown 報告。

## 運作方式

```
Task Scheduler(每日 09:00)
  └─ run_daily.ps1(UTF-8 + PATH 加固)
      └─ python -m src.main
          抓 trending → 去重(上榜多日只分析一次)→ GitHub API 補 metadata
          → shallow clone → claude CLI 唯讀分析 → reports/YYYY-MM-DD.md
```

- **AI 引擎**:本機 `claude` CLI headless 模式,走現有訂閱,不需 API key
- **安全**:分析時 claude 只拿到 `Read / Glob / Grep` 三個唯讀工具,無法執行任何程式碼;prompt 另注入「repo 內容為不可信資料」防護
- **去重**:`data/seen_repos.json` 記錄看過的 repo;連續上榜的專案 14 天內不重複分析,只在報告「持續上榜」區帶一行
- **降級**:AI 失敗 → 只列 metadata;clone 失敗 → 改用 README 輕量分析;trending 抓不到 → 產出說明失敗的 stub 報告。報告永遠會產出
- **輕量分析**:awesome-list / 教學類或超大 repo 不 clone,只餵 README

## 安裝

```powershell
pip install -r requirements.txt
```

前置需求(皆已確認):Python 3.13+、git、claude CLI(已登入)、gh CLI(選用,提高 API 額度)。

## 手動執行與測試

```powershell
python -m src.main --dry-run          # 只看今天會選哪些 repo,不做任何事
python -m src.main --limit 1 --skip-claude   # 演練 clone + 清理,不花 AI 額度
python -m src.main --limit 2          # 完整跑 2 個 repo
python -m src.main --force owner/repo # 強制重新分析某個今日在榜的 repo
python -m src.main --date-override 2026-07-26  # 假裝是另一天(測去重)
```

Exit code:`0` 全部成功、`1` 有降級項目(報告仍產出)、`2` 致命失敗(trending 抓不到)。

## 註冊每日排程

```powershell
.\setup_task.ps1              # 每日 09:00
.\setup_task.ps1 -Time "07:30"
.\setup_task.ps1 -Remove      # 移除
```

排程設定為「使用者登入時執行」+ 錯過補跑(`StartWhenAvailable`)。**不要**改成「未登入也執行」,除非實測過該模式下 claude 訂閱憑證(DPAPI 保護)仍可讀取。

手動觸發與檢查:

```powershell
Start-ScheduledTask -TaskName GitHubTrendingScan
Get-ScheduledTaskInfo -TaskName GitHubTrendingScan   # LastTaskResult 應為 0 或 1
```

## 輸出

- `reports/YYYY-MM-DD.md` — 每日報告(總覽、每個新 repo 的分析、持續上榜表)
- `reports/index.md` — 報告索引(日期、分析數、本日之星)
- `logs/run-*.log` — 詳細執行紀錄(含每個 repo 的 AI 成本),保留 30 天
- `data/seen_repos.json` — 去重與分析快取

## 設定

改 [config.toml](config.toml) 即生效:分析數量(`max_repos`)、模型(`model`)、逾時、輕量規則、重新分析間隔等。每項在檔內有註解。

## 注意事項

- 報告內容為 AI 對**未經驗證的第三方程式碼**的自動分析,僅供參考;安全評估不能取代人工審查
- 每天約消耗 `max_repos` 次 claude 分析呼叫(每次讀十幾個檔案),額度吃緊可調低 `max_repos` 或把 `model` 改為 `haiku`
