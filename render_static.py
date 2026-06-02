#!/usr/bin/env python3
"""生成 docs/index.html 静态快照 → 给 GitHub Pages 用.
持仓金额隐去, API 按钮屏蔽; 仅供朋友看技术分析."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# 复用主程序的逻辑
from mag7_dashboard import build_payload, HTML_TMPL, load_watchlist

DOCS = REPO / "docs"
OUT = DOCS / "index.html"

def render():
    DOCS.mkdir(exist_ok=True)
    data, ts = build_payload()
    wl = load_watchlist()
    payload = json.dumps({
        "data": data,
        "ts": ts,
        "watchlist": [{"code": c, "name": n} for c, n in wl],
        "positions": {},          # 永不暴露
        "public_mode": True,      # 触发前端公开模式
    }, ensure_ascii=False)
    html = HTML_TMPL.replace("__BOOTSTRAP__", payload)
    OUT.write_text(html, encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"[snapshot] {OUT} 写入 {size_kb:.0f} KB · {len(data)} 只 · {ts}")
    return OUT

def git_push():
    """如果是 git 仓库, 自动 add+commit+push."""
    if not (REPO / ".git").exists():
        print("[git] 不是 git 仓库, 跳过 push")
        return
    msg = f"snapshot {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    try:
        subprocess.check_call(["git", "-C", str(REPO), "add", "docs/index.html"])
        # 没改动则 commit 会失败, 容忍
        r = subprocess.run(["git", "-C", str(REPO), "commit", "-m", msg],
                           capture_output=True, text=True)
        if r.returncode != 0:
            if "nothing to commit" in (r.stdout + r.stderr):
                print("[git] 无变化, 跳过")
                return
            print("[git] commit 失败:", r.stderr.strip()); return
        subprocess.check_call(["git", "-C", str(REPO), "push"])
        print(f"[git] pushed: {msg}")
    except subprocess.CalledProcessError as e:
        print(f"[git] 失败: {e}")

if __name__ == "__main__":
    render()
    if "--push" in sys.argv or os.environ.get("DASHBOARD_PUSH") == "1":
        git_push()
