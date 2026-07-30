"""清掉去重檔中已存快取分析裡的結構化輸出殘渣。

模型偶爾把 "</summary><parameter name=...>" 這類外框標記寫進字串欄位。
analyzer.strip_scaffolding 已在取用時擋掉,但先前存進快取的仍需清一次
—— 否則重繪報告時會被跳脫成可見的垃圾文字。

用法:
    python scripts/clean_cached_analyses.py --check   # 只列出,不寫檔
    python scripts/clean_cached_analyses.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzer import strip_scaffolding  # noqa: E402
from src.util import atomic_write_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理快取分析中的輸出殘渣")
    ap.add_argument("--check", action="store_true", help="只檢查,不寫檔")
    args = ap.parse_args(argv)

    store_path = ROOT / "data" / "seen_repos.json"
    data = json.loads(store_path.read_text(encoding="utf-8-sig"))

    changed = 0
    for name, entry in data.items():
        analysis = entry.get("analysis")
        if not isinstance(analysis, dict):
            continue
        cleaned = strip_scaffolding(analysis)
        if cleaned == analysis:
            continue
        changed += 1
        for field, before in analysis.items():
            after = cleaned.get(field)
            if after != before:
                print(f"  {name} → {field}")
                print(f"      前:…{str(before)[-90:]!r}")
                print(f"      後:…{str(after)[-90:]!r}")
        if not args.check:
            entry["analysis"] = cleaned

    if changed and not args.check:
        atomic_write_json(store_path, data)
        print(f"\n已清理 {changed} 筆並存檔")
    elif changed:
        print(f"\n共 {changed} 筆需要清理(--check 模式未寫檔)")
    else:
        print("沒有需要清理的快取")
    return 0


if __name__ == "__main__":
    sys.exit(main())
