#!/usr/bin/env python3
"""Mag7 AI 行情看板 - 通过 FutuOpenD 实时拉取数据生成单文件 HTML 看板."""
import json
import math
import os
import subprocess
import threading
import warnings
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from curl_cffi import requests as cffi_requests
from futu import OpenQuoteContext, RET_OK, KLType, AuType

warnings.filterwarnings("ignore")

# 复用一个伪装浏览器的 session 给 yfinance，绕 Yahoo 限流
YF_SESSION = cffi_requests.Session(impersonate="chrome")

# 简易缓存：分析师/新闻 30 分钟刷新一次，避免被限流
_YF_CACHE = {"ts": 0, "data": {}}
_YF_TTL = 30 * 60  # 秒

HOST, PORT = "127.0.0.1", 11111
SERVE_PORT = 8765
OUT = Path.home() / "Desktop" / "mag7_dashboard.html"
WATCHLIST_FILE = Path.home() / ".watchlist_dashboard.json"
US_UNIVERSE_FILE = Path.home() / ".us_stock_universe.json"
POSITIONS_FILE = Path.home() / ".positions_dashboard.json"
MAX_WATCHLIST = 20  # 防止 yfinance 限流，自选最多 20 只

DEFAULT_WATCHLIST = [
    ("US.NVDA",  "NVIDIA"),
    ("US.MSFT",  "Microsoft"),
    ("US.GOOGL", "Alphabet"),
    ("US.META",  "Meta"),
    ("US.AMZN",  "Amazon"),
    ("US.AAPL",  "Apple"),
    ("US.TSLA",  "Tesla"),
]

# ---------------- 自选股 + 搜索 ----------------
def load_watchlist():
    """读取自选；不存在则用默认 Mag7."""
    if WATCHLIST_FILE.exists():
        try:
            wl = json.loads(WATCHLIST_FILE.read_text())
            if isinstance(wl, list) and wl:
                return [(x["code"], x["name"]) for x in wl]
        except Exception:
            pass
    save_watchlist(DEFAULT_WATCHLIST)
    return list(DEFAULT_WATCHLIST)

def save_watchlist(wl):
    WATCHLIST_FILE.write_text(
        json.dumps([{"code": c, "name": n} for c, n in wl], ensure_ascii=False, indent=2)
    )

def load_positions():
    """持仓数据：{code: {shares, cost}}"""
    if POSITIONS_FILE.exists():
        try:
            return json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_positions(pos):
    POSITIONS_FILE.write_text(json.dumps(pos, ensure_ascii=False, indent=2))

def load_universe():
    """美股全列表：本地缓存 24h，否则从 Futu 拉。"""
    import time as _t
    if US_UNIVERSE_FILE.exists() and (_t.time() - US_UNIVERSE_FILE.stat().st_mtime < 86400):
        try:
            return json.loads(US_UNIVERSE_FILE.read_text())
        except Exception:
            pass
    print("[universe] 从 Futu 拉取美股全列表 (一次性, 缓存 24h)...")
    from futu import Market, SecurityType
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    try:
        ret, df = ctx.get_stock_basicinfo(Market.US, SecurityType.STOCK)
        if ret != RET_OK:
            raise RuntimeError(f"universe 拉取失败: {df}")
        items = [{"code": r["code"], "name": r["name"]} for _, r in df.iterrows()]
    finally:
        ctx.close()
    US_UNIVERSE_FILE.write_text(json.dumps(items, ensure_ascii=False))
    print(f"[universe] 缓存 {len(items)} 只美股")
    return items

_UNIVERSE_CACHE = None
def search_stocks(q, limit=20):
    global _UNIVERSE_CACHE
    if _UNIVERSE_CACHE is None:
        _UNIVERSE_CACHE = load_universe()
    if not q: return []
    q = q.strip().upper()
    if not q: return []
    # 精确代码命中优先；其次代码前缀；最后名称包含
    exact, prefix, contains = [], [], []
    for it in _UNIVERSE_CACHE:
        code_short = it["code"].replace("US.", "")
        name_up = it["name"].upper()
        if code_short == q:
            exact.append(it)
        elif code_short.startswith(q):
            prefix.append(it)
        elif q in code_short or q in name_up:
            contains.append(it)
        if len(exact) + len(prefix) + len(contains) > limit * 3:
            break
    return (exact + prefix + contains)[:limit]

# ---------------- 数据拉取 ----------------
def fetch_data(watchlist):
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    codes = [c for c, _ in watchlist]
    ret, snap = ctx.get_market_snapshot(codes)
    if ret != RET_OK:
        ctx.close(); raise RuntimeError(f"snapshot 失败: {snap}")

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=130)).strftime("%Y-%m-%d")  # 留出非交易日缓冲
    klines = {}
    for code in codes:
        ret, df, _ = ctx.request_history_kline(
            code, start=start, end=end,
            ktype=KLType.K_DAY, autype=AuType.QFQ,
            max_count=200,
        )
        if ret != RET_OK:
            ctx.close(); raise RuntimeError(f"{code} K线失败: {df}")
        df["time_key"] = pd.to_datetime(df["time_key"])
        df = df.sort_values("time_key").reset_index(drop=True)
        # 只保留近 ~63 个交易日 (3M)
        klines[code] = df.tail(63).reset_index(drop=True)
    ctx.close()
    return snap, klines

# ---------------- 舆论 + 分析师 (yfinance) ----------------
POS_KW = {"surge","jump","rise","rises","beat","beats","gain","gains","rally","soar","soars","upgrade","bullish",
          "growth","record","strong","boost","exceed","outperform","top","tops","high","wins","win","accelerate",
          "expand","breakthrough","milestone","launch","unveil","leads","leader","dominant","optimistic"}
NEG_KW = {"fall","falls","drop","drops","plunge","plunges","miss","misses","decline","downgrade","bearish","warning",
          "cut","cuts","layoff","layoffs","lawsuit","probe","weak","struggle","concern","slump","tumble","sink",
          "loss","losses","slow","slows","slowdown","disappoint","risk","risks","threat","investigation","ban","sue"}

def score_text(t):
    if not t: return 0
    s = t.lower()
    p = sum(1 for w in POS_KW if w in s)
    n = sum(1 for w in NEG_KW if w in s)
    return p - n

def fetch_yf(symbol, retries=2):
    """单只标的：拉 info + news；遇限流自动退避重试."""
    import time as _t
    sym = symbol.replace("US.", "")
    info, news_raw, last_err = None, None, None
    for i in range(retries + 1):
        try:
            t = yf.Ticker(sym, session=YF_SESSION)
            info = t.info or {}
            news_raw = (t.news or [])[:6]
            last_err = None
            break
        except Exception as e:
            last_err = str(e)
            if "Too Many Requests" in last_err or "Rate" in last_err:
                _t.sleep(3 * (i + 1))  # 3s, 6s 退避
            else:
                break
    if last_err:
        return {"sym": sym, "error": last_err, "news": [], "analyst": {}}
    info = info or {}
    news_raw = news_raw or []

    news = []
    for n in news_raw:
        c = n.get("content") or n
        title = c.get("title", "")
        if not title: continue
        sc = score_text(title + " " + (c.get("summary") or ""))
        news.append({
            "title": title,
            "summary": (c.get("summary") or "")[:160],
            "url": ((c.get("canonicalUrl") or c.get("clickThroughUrl") or {}) or {}).get("url", ""),
            "provider": ((c.get("provider") or {}).get("displayName") or ""),
            "pub": (c.get("pubDate") or c.get("displayTime") or "")[:10],
            "sent": sc,
        })

    def g(k, default=None):
        v = info.get(k)
        return default if v in (None, "", "Infinity") else v

    analyst = {
        "tgt_mean":   g("targetMeanPrice"),
        "tgt_high":   g("targetHighPrice"),
        "tgt_low":    g("targetLowPrice"),
        "tgt_median": g("targetMedianPrice"),
        "n_analyst":  g("numberOfAnalystOpinions", 0),
        "rec_key":    g("recommendationKey", "n/a"),
        "rec_mean":   g("recommendationMean"),    # 1=Strong Buy ... 5=Sell
        "fwd_pe":     g("forwardPE"),
        "trail_pe":   g("trailingPE"),
        "eps_g":      g("earningsGrowth"),        # 同比
        "rev_g":      g("revenueGrowth"),
        "margin":     g("profitMargins"),
        "beta":       g("beta"),
        "sector":     g("sector", ""),
        "industry":   g("industry", ""),
    }
    return {"sym": sym, "news": news, "analyst": analyst}

def fetch_yf_all(codes, force=False):
    """串行拉取，带 30 分钟缓存。分析师/新闻日内变化小，无需每次刷新都打 Yahoo.
    若全部失败 (限流) 不污染缓存，下次刷新仍会重试.
    若部分成功，合并旧缓存中已有的成功结果一起返回."""
    import time as _t
    now = _t.time()
    if not force and (now - _YF_CACHE["ts"] < _YF_TTL) and _YF_CACHE["data"]:
        cached = _YF_CACHE["data"]
        # 仅覆盖当前自选股，且缓存里也有成功的项
        if all(c.replace("US.","") in cached and not cached[c.replace("US.","")].get("error") for c in codes):
            return cached
    print(f"[yfinance] 拉取 {len(codes)} 只标的的分析师+新闻 (间隔 2s)...")
    out, ok, fail = {}, 0, 0
    for i, code in enumerate(codes):
        r = fetch_yf(code)
        out[r["sym"]] = r
        if r.get("error"):
            fail += 1
            print(f"[yfinance] {r['sym']} 失败: {r['error']}")
        else:
            ok += 1
        if i < len(codes) - 1:
            _t.sleep(2.0)  # Yahoo 限流防护
    print(f"[yfinance] 完成: {ok} 成功 / {fail} 失败")
    # 合并已有缓存中"成功"的项 (用旧成功覆盖新失败)
    for sym, r in (_YF_CACHE.get("data") or {}).items():
        if sym in out and out[sym].get("error") and not r.get("error"):
            out[sym] = r
    # 至少一半成功才更新缓存时间戳，否则下次仍会重试
    if ok >= max(1, len(codes) // 2):
        _YF_CACHE["ts"] = now
    _YF_CACHE["data"] = out
    return out

# ---------------- 指标计算 ----------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = -d.clip(upper=0).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def macd(close, fast=12, slow=26, sig=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=sig, adjust=False).mean()
    return dif, dea, (dif - dea) * 2

def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(h - l), (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def compute(df, snap_row):
    c = df["close"]
    ret_log = np.log(c / c.shift(1))
    vol_annual = ret_log.std() * math.sqrt(252) * 100  # %

    ma20 = c.rolling(20).mean()
    ma50 = c.rolling(50).mean()
    std20 = c.rolling(20).std()
    bb_up, bb_dn = ma20 + 2 * std20, ma20 - 2 * std20

    rsi14 = rsi(c, 14)
    dif, dea, hist = macd(c)
    atr14 = atr(df, 14)

    last = float(c.iloc[-1])
    chg1d = (last / c.iloc[-2] - 1) * 100 if len(c) > 1 else 0
    chg1w = (last / c.iloc[-6] - 1) * 100 if len(c) > 6 else 0
    chg1m = (last / c.iloc[-21] - 1) * 100 if len(c) > 21 else 0
    chg3m = (last / c.iloc[0] - 1) * 100

    hi3m, lo3m = float(df["high"].max()), float(df["low"].min())
    pos_in_range = (last - lo3m) / (hi3m - lo3m) * 100 if hi3m > lo3m else 50

    vol_avg20 = float(df["volume"].tail(20).mean())
    vol_today = float(df["volume"].iloc[-1])
    vol_ratio = vol_today / vol_avg20 if vol_avg20 else 1.0

    bb_pos = (last - bb_dn.iloc[-1]) / (bb_up.iloc[-1] - bb_dn.iloc[-1]) * 100 if bb_up.iloc[-1] > bb_dn.iloc[-1] else 50

    # ----- 信号打分 (-3 ~ +3) -----
    score, reasons = 0, []
    r = float(rsi14.iloc[-1]) if not math.isnan(rsi14.iloc[-1]) else 50
    if r < 30:  score += 1; reasons.append(f"RSI={r:.0f} 超卖")
    elif r > 70: score -= 1; reasons.append(f"RSI={r:.0f} 超买")
    else: reasons.append(f"RSI={r:.0f} 中性")

    if hist.iloc[-1] > 0 and hist.iloc[-2] <= 0:
        score += 2; reasons.append("MACD 金叉")
    elif hist.iloc[-1] < 0 and hist.iloc[-2] >= 0:
        score -= 2; reasons.append("MACD 死叉")
    elif hist.iloc[-1] > 0:
        score += 0.5; reasons.append("MACD 多头")
    else:
        score -= 0.5; reasons.append("MACD 空头")

    if last > ma20.iloc[-1] > ma50.iloc[-1]:
        score += 1; reasons.append("均线多头排列")
    elif last < ma20.iloc[-1] < ma50.iloc[-1]:
        score -= 1; reasons.append("均线空头排列")

    if bb_pos > 95: score -= 0.5; reasons.append("触及布林上轨")
    elif bb_pos < 5: score += 0.5; reasons.append("触及布林下轨")

    if vol_ratio > 1.5 and chg1d > 0:
        score += 0.5; reasons.append(f"放量上涨 ({vol_ratio:.1f}×)")
    elif vol_ratio > 1.5 and chg1d < 0:
        score -= 0.5; reasons.append(f"放量下跌 ({vol_ratio:.1f}×)")

    if score >= 2.5:    signal, color = "强烈买入", "#16a34a"
    elif score >= 1:    signal, color = "买入",     "#22c55e"
    elif score > -1:    signal, color = "中性",     "#94a3b8"
    elif score > -2.5:  signal, color = "卖出",     "#f59e0b"
    else:               signal, color = "强烈卖出", "#ef4444"

    # ----- 止损 / 止盈 建议（多种流派取参考价） -----
    atr_v = float(atr14.iloc[-1]) if not math.isnan(atr14.iloc[-1]) else last * 0.02
    sl_oneil = last * 0.93        # William O'Neil 7% 铁律
    sl_atr   = last - 2 * atr_v   # ATR 法 (2×ATR)
    sl_ma50  = float(ma50.iloc[-1]) if not math.isnan(ma50.iloc[-1]) else last * 0.9
    sl_3m_lo = float(df["low"].tail(20).min())  # 近月低点
    # 综合止损：取以上几种中"最高"的（最先触发的），更保守
    sl_final = max(sl_oneil, sl_atr, sl_ma50 * 0.98)  # MA50 下方 2% 兜底
    tp_1 = last * 1.20            # 第一档止盈（+20%）
    tp_2 = last * 1.50            # 第二档止盈（+50%）

    suggestions = {
        "sl_oneil": sl_oneil, "sl_atr": sl_atr, "sl_ma50": sl_ma50, "sl_3m_lo": sl_3m_lo,
        "sl_final": sl_final, "sl_pct": (sl_final/last - 1) * 100,
        "tp1": tp_1, "tp2": tp_2,
    }

    return {
        "last": last, "chg1d": chg1d, "chg1w": chg1w, "chg1m": chg1m, "chg3m": chg3m,
        "hi3m": hi3m, "lo3m": lo3m, "pos_in_range": pos_in_range,
        "vol_today": vol_today, "vol_avg20": vol_avg20, "vol_ratio": vol_ratio,
        "vol_annual": vol_annual,
        "ma20": float(ma20.iloc[-1]), "ma50": float(ma50.iloc[-1]),
        "bb_up": float(bb_up.iloc[-1]), "bb_dn": float(bb_dn.iloc[-1]), "bb_pos": bb_pos,
        "rsi": r, "macd_hist": float(hist.iloc[-1]),
        "atr": float(atr14.iloc[-1]),
        "pe": float(snap_row.get("pe_ratio", 0) or 0),
        "pb": float(snap_row.get("pb_ratio", 0) or 0),
        "mcap": float(snap_row.get("total_market_val", 0) or 0),
        "turnover": float(snap_row.get("turnover", 0) or 0),
        "score": round(score, 1), "signal": signal, "color": color, "reasons": reasons,
        "sug": suggestions,
        "dates": [d.strftime("%m-%d") for d in df["time_key"]],
        "ohlc": [[float(o), float(c_), float(l), float(h)] for o, c_, l, h in
                 zip(df["open"], df["close"], df["low"], df["high"])],
        "volumes": [float(v) for v in df["volume"]],
        "ma20_line": [None if pd.isna(v) else float(v) for v in ma20],
        "ma50_line": [None if pd.isna(v) else float(v) for v in ma50],
    }

def pricing_logic(last, ana):
    """生成"为什么这个目标价"的文字逻辑."""
    if not ana.get("tgt_mean"): return "暂无分析师覆盖"
    tgt = ana["tgt_mean"]; upside = (tgt/last - 1)*100
    rec = {"strong_buy":"强烈买入","buy":"买入","hold":"中性","underperform":"减持","sell":"卖出"}.get(ana.get("rec_key"), ana.get("rec_key") or "n/a")
    parts = [f"{ana.get('n_analyst',0)}位分析师给出共识 [{rec}] (评分 {ana.get('rec_mean',0):.2f}/5, 越低越多头)"]
    parts.append(f"目标价区间 ${ana.get('tgt_low',0):.0f} – ${ana.get('tgt_high',0):.0f}，均价 ${tgt:.1f} ({upside:+.1f}% vs 现价)")
    if ana.get("fwd_pe") and ana.get("trail_pe"):
        trend = "估值消化(盈利增长快于股价)" if ana["fwd_pe"] < ana["trail_pe"]*0.85 else "估值扩张" if ana["fwd_pe"] > ana["trail_pe"]*1.05 else "估值持平"
        parts.append(f"远期PE {ana['fwd_pe']:.1f}× vs 当前PE {ana['trail_pe']:.1f}× → {trend}")
    if ana.get("eps_g") is not None and ana.get("rev_g") is not None:
        parts.append(f"盈利同比 {ana['eps_g']*100:+.0f}% / 营收 {ana['rev_g']*100:+.0f}% / 净利率 {(ana.get('margin') or 0)*100:.0f}%")
    return " · ".join(parts)

# ---------------- 静态快照 (手动触发, 推 GitHub Pages) ----------------
REPO_DIR = Path(__file__).resolve().parent
PAGES_URL = "https://cicidiao-hub.github.io/mag7-dashboard/"
_SNAPSHOT = {"running": False, "last_ts": None, "last_git": "", "last_err": None, "lock": threading.Lock()}

def run_snapshot():
    """生成 docs/index.html (持仓隐藏) + git add/commit/push. 阻塞至完成."""
    docs = REPO_DIR / "docs"
    docs.mkdir(exist_ok=True)
    out = docs / "index.html"

    data, ts = build_payload()
    wl = load_watchlist()
    payload = json.dumps({
        "data": data, "ts": ts,
        "watchlist": [{"code": c, "name": n} for c, n in wl],
        "positions": {},        # 永不暴露持仓
        "public_mode": True,
    }, ensure_ascii=False)
    out.write_text(HTML_TMPL.replace("__BOOTSTRAP__", payload), encoding="utf-8")
    size_kb = round(out.stat().st_size / 1024, 1)
    print(f"[snapshot] {out} 写入 {size_kb} KB · {len(data)} 只 · {ts}")

    git_status = "no-git"
    if (REPO_DIR / ".git").exists():
        try:
            subprocess.check_call(["git", "-C", str(REPO_DIR), "add", "docs/index.html"])
            msg = f"snapshot {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            r = subprocess.run(["git", "-C", str(REPO_DIR), "commit", "-m", msg],
                               capture_output=True, text=True)
            if r.returncode == 0:
                p = subprocess.run(["git", "-C", str(REPO_DIR), "push"],
                                   capture_output=True, text=True, timeout=60)
                git_status = "pushed ✓" if p.returncode == 0 else f"push 失败: {p.stderr.strip()[:120]}"
            elif "nothing to commit" in (r.stdout + r.stderr):
                git_status = "无变化"
            else:
                git_status = f"commit 失败: {r.stderr.strip()[:120]}"
        except subprocess.TimeoutExpired:
            git_status = "push 超时 (60s)"
        except subprocess.CalledProcessError as e:
            git_status = f"git 错误: {e}"
    print(f"[snapshot] git: {git_status}")
    return {"ts": ts, "size_kb": size_kb, "git": git_status, "pages_url": PAGES_URL}

def build_payload():
    watchlist = load_watchlist()
    snap, klines = fetch_data(watchlist)
    snap_idx = snap.set_index("code")
    codes = [c for c, _ in watchlist]
    yf_data = fetch_yf_all(codes)

    out = []
    for code, name in watchlist:
        m = compute(klines[code], snap_idx.loc[code].to_dict())
        m["code"], m["name"] = code, name
        sym = code.replace("US.", "")
        y = yf_data.get(sym, {})
        m["analyst"] = y.get("analyst", {})
        m["news"] = y.get("news", [])
        # 舆论分：取近 6 条标题情感分均值
        sents = [n["sent"] for n in m["news"]]
        m["sent_avg"] = round(sum(sents)/len(sents), 2) if sents else 0
        m["sent_label"] = "看多" if m["sent_avg"] > 0.4 else "看空" if m["sent_avg"] < -0.4 else "中性"
        # 上涨空间
        a = m["analyst"]
        if a.get("tgt_mean"):
            m["upside"] = (a["tgt_mean"]/m["last"] - 1) * 100
        else:
            m["upside"] = None
        m["pricing_logic"] = pricing_logic(m["last"], a)
        out.append(m)
    return out, datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- HTML ----------------
HTML_TMPL = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>自选股分析看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  *{box-sizing:border-box} body{margin:0;font-family:-apple-system,"PingFang SC",sans-serif;background:#0b1020;color:#e2e8f0}
  header{padding:20px 28px;background:linear-gradient(135deg,#1e293b,#0f172a);border-bottom:1px solid #1e293b;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap}
  h1{margin:0;font-size:22px;font-weight:600} .meta{color:#94a3b8;font-size:13px}
  .container{padding:20px 28px;max-width:1700px;margin:0 auto}
  .grid{display:grid;gap:16px} .summary{grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin-bottom:24px}
  .card{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:14px 16px;position:relative;overflow:hidden}
  .card .sig{position:absolute;top:0;right:0;padding:3px 10px;font-size:11px;font-weight:600;border-bottom-left-radius:8px;color:#fff}
  .card .sym{font-size:13px;color:#94a3b8} .card .nm{font-size:16px;font-weight:600;margin-top:2px}
  .card .px{font-size:24px;font-weight:700;margin:6px 0 2px}
  .card .row{display:flex;justify-content:space-between;font-size:12px;color:#cbd5e1;margin-top:4px}
  .up{color:#22c55e} .dn{color:#ef4444} .nu{color:#94a3b8}
  .section-title{font-size:15px;font-weight:600;margin:20px 0 10px;color:#e2e8f0;padding-left:8px;border-left:3px solid #3b82f6}
  table{width:100%;border-collapse:collapse;background:#111827;border-radius:10px;overflow:hidden;font-size:13px}
  th,td{padding:10px 12px;text-align:right;border-bottom:1px solid #1f2937}
  th{background:#0f172a;color:#94a3b8;font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  th:first-child,td:first-child{text-align:left} tr:hover{background:#1a2236}
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;color:#fff}
  .charts{grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
  .chart-card{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:10px}
  .chart-card h3{margin:4px 8px;font-size:13px;color:#e2e8f0}
  .chart{height:280px} .bar-chart{height:300px}
  .reasons{font-size:11px;color:#94a3b8;margin-top:6px}
  .legend-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}
  .disclaimer{color:#64748b;font-size:11px;margin-top:24px;text-align:center;padding:12px;border-top:1px solid #1f2937}
  .toolbar{display:flex;align-items:center;gap:12px}
  .news-list{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:8px 14px}
  .news-item{padding:8px 0;border-bottom:1px solid #1f2937;font-size:12px;display:grid;grid-template-columns:50px 1fr auto;gap:10px;align-items:start}
  .news-item:last-child{border-bottom:none}
  .news-item .sent{font-size:11px;font-weight:600;text-align:center;padding:2px 6px;border-radius:4px;align-self:center}
  .news-item a{color:#e2e8f0;text-decoration:none} .news-item a:hover{color:#60a5fa;text-decoration:underline}
  .news-item .src{color:#64748b;font-size:11px;margin-top:2px}
  .stock-block{margin-bottom:14px}
  .stock-block h4{margin:6px 0;font-size:13px;color:#cbd5e1;display:flex;align-items:center;gap:8px}
  .gauge{display:inline-block;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600}
  details summary{cursor:pointer;color:#94a3b8;font-size:12px;padding:6px 0}
  details[open] summary{color:#e2e8f0}
  .pricing-card{background:#0f172a;border-left:3px solid #3b82f6;padding:10px 14px;border-radius:6px;margin:6px 0;font-size:12px;color:#cbd5e1;line-height:1.7}
  .pricing-card b{color:#e2e8f0}
  .btn{background:#3b82f6;color:#fff;border:none;padding:7px 16px;border-radius:6px;font-size:13px;cursor:pointer;font-weight:500;display:inline-flex;align-items:center;gap:6px;transition:background .15s}
  .btn:hover{background:#2563eb} .btn:disabled{background:#475569;cursor:not-allowed}
  .btn .spin{display:inline-block;width:12px;height:12px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:sp 0.7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  #ago{color:#cbd5e1;font-size:12px;font-variant-numeric:tabular-nums}
  .search-wrap{position:relative}
  .search-wrap input{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:7px 12px;border-radius:6px;width:260px;font-size:13px;outline:none}
  .search-wrap input:focus{border-color:#3b82f6}
  .search-results{position:absolute;top:38px;right:0;background:#0f172a;border:1px solid #334155;border-radius:6px;max-height:340px;overflow-y:auto;width:380px;z-index:1000;display:none;box-shadow:0 10px 30px rgba(0,0,0,.4)}
  .search-results.show{display:block}
  .sr-item{padding:9px 14px;cursor:pointer;border-bottom:1px solid #1f2937;font-size:13px;display:flex;justify-content:space-between;align-items:center;gap:10px}
  .sr-item:hover{background:#1e293b} .sr-item:last-child{border-bottom:none}
  .sr-item .sr-code{color:#60a5fa;font-weight:600;min-width:70px}
  .sr-item .sr-name{color:#cbd5e1;flex:1;text-align:left}
  .sr-item .sr-add{color:#22c55e;font-size:18px}
  .sr-empty{padding:14px;color:#64748b;font-size:13px;text-align:center}
  .card .rm{position:absolute;bottom:6px;right:8px;background:transparent;border:none;color:#475569;font-size:14px;cursor:pointer;padding:2px 8px;border-radius:4px;transition:all .15s}
  .card .rm:hover{background:#ef4444;color:#fff}
  .limit-note{font-size:11px;color:#fbbf24;background:rgba(245,158,11,.08);border-left:3px solid #f59e0b;padding:9px 14px;border-radius:6px;margin-bottom:14px;line-height:1.6}
  .btn-ghost{background:transparent;color:#94a3b8;border:1px solid #334155;padding:6px 12px;font-size:12px}
  .btn-ghost:hover{background:#1e293b;color:#e2e8f0}
  .rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;margin-bottom:14px}
  .rule{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:12px 14px;border-top:3px solid #ef4444}
  .rule h4{margin:0 0 6px;font-size:13px;color:#fca5a5}
  .rule ul{margin:0;padding-left:18px;font-size:12px;color:#cbd5e1;line-height:1.6}
  .rule li{margin-bottom:3px}
  .portfolio{background:#0f172a;border:1px solid #1f2937;border-radius:10px;padding:14px 18px;margin-bottom:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
  .portfolio .item{text-align:center}
  .portfolio .label{font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px}
  .portfolio .val{font-size:20px;font-weight:700;margin-top:2px}
  .pos-panel{background:#0f172a;border-top:1px solid #1f2937;margin-top:8px;padding:8px;border-radius:0 0 8px 8px;font-size:11px}
  .pos-panel .pos-row{display:grid;grid-template-columns:55px 1fr;gap:6px;align-items:center;margin-bottom:4px}
  .pos-panel input{background:#0b1020;border:1px solid #334155;color:#e2e8f0;padding:3px 6px;border-radius:4px;width:100%;font-size:11px;outline:none}
  .pos-panel input:focus{border-color:#3b82f6}
  .pos-panel .pos-stat{display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:#94a3b8}
  .pos-panel .pos-stat b{color:#e2e8f0;font-size:12px}
  .sl-tp-table th, .sl-tp-table td{font-size:12px;padding:8px 10px}
</style></head>
<body>
<header>
  <div>
    <h1>自选股分析看板 <span style="font-size:12px;color:#94a3b8;font-weight:400">(<span id="wl_count">0</span> 只)</span></h1>
    <div class="meta">数据源 FutuOpenD + Yahoo Finance · 近3个月 · 更新 <span id="ts">-</span> · <span id="ago">刚刚</span> · 综合信号 <span id="overall"></span> · 📸 <span id="snap_state">快照未推</span></div>
  </div>
  <div class="toolbar">
    <div class="search-wrap">
      <input id="searchInput" placeholder="搜索代码或公司名 (如 AMD / palantir)" autocomplete="off"/>
      <div id="searchResults" class="search-results"></div>
    </div>
    <button class="btn btn-ghost" onclick="resetWatchlist()" title="恢复默认 Mag7">↺ 默认</button>
    <button id="snapBtn" class="btn btn-ghost" onclick="pushSnapshot()" title="生成公开版快照并推 GitHub Pages">📸 推快照</button>
    <button id="refreshBtn" class="btn" onclick="refresh()"><span>⟳</span><span>刷新</span></button>
  </div>
</header>
<div class="container">
  <div class="limit-note">
    ⓘ <b>舆论数据局限</b>：新闻取自 Yahoo Finance 按 ticker 关联的近 6 条头条，可能包含竞品/行业稿；情感打分基于英文正负词典，无法识别否定/反讽。仅作快速参考，独立下单请用专业金融 NLP + 多源 (Reuters/Bloomberg/FT)。
  </div>

  <div class="section-title">交易铁律 (常见流派红线 · 参考)</div>
  <div class="rules">
    <div class="rule"><h4>🛑 止损铁律</h4><ul>
      <li><b>O'Neil 7% 法</b>：买入价跌 7% 无条件清仓，永不破例</li>
      <li><b>ATR 法</b>：跌破 入场价 − 2×ATR(14)</li>
      <li><b>技术法</b>：跌破 MA50 或前期重要支撑</li>
      <li><b>时间法</b>：3 月未明显趋势 → 退出找更好标的</li>
    </ul></div>
    <div class="rule"><h4>💰 止盈分批</h4><ul>
      <li><b>+20%</b> 卖 1/3 锁利</li>
      <li><b>+50%</b> 再卖 1/3</li>
      <li><b>剩余</b> 用 trailing stop 跟高点 −10%</li>
      <li>到分析师目标价附近优先减仓而非追涨</li>
    </ul></div>
    <div class="rule"><h4>📊 仓位红线</h4><ul>
      <li>单只 ≤ <b>10%</b> (集中仓 Buffett 风格)，<b>5%</b> (分散)</li>
      <li>单板块 ≤ <b>30%</b>，避免行业塌方</li>
      <li>现金保留 ≥ <b>10%</b> 应对回调</li>
      <li><b>禁用杠杆</b>炒个股，禁裸卖期权</li>
    </ul></div>
    <div class="rule"><h4>⛔ 操作禁区</h4><ul>
      <li><b>不补亏损股</b> (Don't average down)</li>
      <li><b>不接飞刀</b>：单日跌 &gt;5% 当日不抄底</li>
      <li><b>财报前减仓至 1/2</b>，避免单日 20% 波动</li>
      <li>连续两季盈利下滑 → 退出</li>
    </ul></div>
  </div>

  <div class="section-title">持仓汇总 (本地存储 + 服务器同步)</div>
  <div class="portfolio" id="portfolio_box"></div>

  <div class="section-title">自选汇总卡片 (填写仓位+成本 / × 删除自选)</div>
  <div class="grid summary" id="cards"></div>

  <div class="section-title">明细对比</div>
  <table id="tbl"><thead><tr>
    <th>标的</th><th>最新</th><th>当日%</th><th>1W%</th><th>1M%</th><th>3M%</th>
    <th>区间位置</th><th>量比</th><th>年化波动率</th><th>RSI14</th><th>PE</th><th>市值(B)</th><th>信号</th>
  </tr></thead><tbody></tbody></table>

  <div class="section-title">横向对比</div>
  <div class="grid charts">
    <div class="chart-card"><h3>3个月涨跌幅 (%)</h3><div id="cmp_chg" class="bar-chart"></div></div>
    <div class="chart-card"><h3>年化波动率 (%) · 越高风险越大</h3><div id="cmp_vol" class="bar-chart"></div></div>
    <div class="chart-card"><h3>量比 (今日/20日均) · &gt;1.5 异动</h3><div id="cmp_vr" class="bar-chart"></div></div>
    <div class="chart-card"><h3>市盈率 PE</h3><div id="cmp_pe" class="bar-chart"></div></div>
  </div>

  <div class="section-title">K线 · MA · 成交量 (近3个月)</div>
  <div class="grid charts" id="klines"></div>

  <div class="section-title">交易信号解读 (技术面)</div>
  <table><thead><tr><th>标的</th><th>评分</th><th>建议</th><th>主要依据</th><th>关键位 (支撑 / 阻力)</th></tr></thead>
  <tbody id="sig_tbl"></tbody></table>

  <div class="section-title">分析师定价 (华尔街共识)</div>
  <table id="ana_tbl"><thead><tr>
    <th>标的</th><th>现价</th><th>目标价(均)</th><th>目标区间(低-高)</th><th>上涨空间</th>
    <th>分析师数</th><th>评级</th><th>共识分(1=强买)</th><th>远期PE</th><th>盈利增速</th><th>营收增速</th>
  </tr></thead><tbody></tbody></table>

  <div class="section-title">定价逻辑解读</div>
  <div id="pricing_box"></div>

  <div class="section-title">PE 估值对比 (当前 vs 远期)</div>
  <div class="chart-card"><div id="cmp_pe2" style="height:320px"></div></div>

  <div class="section-title">个性化交易建议 (止损 / 止盈 / 仓位健康)</div>
  <table class="sl-tp-table"><thead><tr>
    <th>标的</th><th>现价</th><th>O'Neil -7%</th><th>2×ATR 止损</th><th>MA50</th><th>建议止损 (最近触发)</th><th>止盈一 +20%</th><th>止盈二 +50%</th><th>分析师目标</th><th class="pos-col">仓位状态</th>
  </tr></thead><tbody id="sltp_tbl"></tbody></table>

  <div class="section-title">板块舆论情绪</div>
  <div class="grid charts">
    <div class="chart-card"><h3>舆论情感分排行 (近6条标题均值)</h3><div id="cmp_sent" class="bar-chart"></div></div>
    <div class="chart-card"><h3>分析师上涨空间 (目标均价 vs 现价 %)</h3><div id="cmp_upside" class="bar-chart"></div></div>
  </div>

  <div class="section-title">最新舆论 (展开查看)</div>
  <div id="news_box"></div>

  <div class="disclaimer">⚠️ 本看板基于公开行情技术指标自动生成，仅供研究参考，不构成投资建议。交易决策请结合基本面、宏观环境与风险承受能力，并自负盈亏。</div>
</div>

<script>
const BOOT = __BOOTSTRAP__;
let D = BOOT.data;
let WL = BOOT.watchlist;
let POS = BOOT.positions || {};   // 服务器持仓
const PUBLIC_MODE = !!BOOT.public_mode;  // 静态快照模式: 隐藏个人持仓 & API 按钮
let lastUpdate = Date.now();
document.getElementById("ts").textContent = BOOT.ts;

if (PUBLIC_MODE) {
  // 公开版: 屏蔽所有写入类按钮 + 持仓相关 UI
  document.querySelectorAll("#refreshBtn, .search-wrap, .btn-ghost").forEach(el=>el.style.display="none");
  const meta = document.querySelector("header .meta");
  if (meta) meta.insertAdjacentHTML("beforeend", ' <span style="color:#fbbf24">· 📷 公开只读快照</span>');
  // 隐藏持仓汇总区块
  const portBox = document.getElementById("portfolio_box");
  if (portBox) {
    portBox.style.display = "none";
    const prev = portBox.previousElementSibling;
    if (prev && prev.classList.contains("section-title")) prev.style.display = "none";
  }
  // 自选汇总卡片改文案
  document.querySelectorAll(".section-title").forEach(t=>{
    if (t.textContent.includes("自选汇总卡片")) t.textContent = "自选股技术面 (公开版)";
  });
}

// localStorage 缓存：在服务器同步之前先用本地的，加速首屏
const LS_KEY = "dashboard_positions_v1";
try{
  const lsPos = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
  // 服务器为准；只有服务器没有时才用本地
  if(Object.keys(POS).length === 0 && Object.keys(lsPos).length > 0) POS = lsPos;
}catch(e){}
function persistLocal(){ try{ localStorage.setItem(LS_KEY, JSON.stringify(POS)); }catch(e){} }
persistLocal();
const charts = [];  // 保存所有 echarts 实例以便 dispose
const fmt = (n,d=2)=> n==null||isNaN(n)?"-":Number(n).toLocaleString("en",{maximumFractionDigits:d,minimumFractionDigits:d});
const fmtB = n => n? (n/1e9).toFixed(1)+"B":"-";
const cls = n => n>0?"up":n<0?"dn":"nu";
const arrow = n => n>0?"▲":n<0?"▼":"●";

function render(){
  // 清理旧 chart
  charts.forEach(c=>c.dispose()); charts.length = 0;

  document.getElementById("wl_count").textContent = D.length;
  // 汇总卡片 + 持仓输入
  const cards = document.getElementById("cards"); cards.innerHTML = "";
  D.forEach(s=>{
    const p = POS[s.code] || {shares:0, cost:0};
    const mv = p.shares * s.last;
    const cb = p.shares * (p.cost || 0);
    const pnl = mv - cb;
    const pnlPct = cb>0 ? (pnl/cb)*100 : 0;
    const sl = s.sug.sl_final;
    const distSL = ((s.last - sl) / s.last) * 100;  // 距止损的下跌空间
    cards.insertAdjacentHTML("beforeend",`
      <div class="card">
        <span class="sig" style="background:${s.color}">${s.signal}</span>
        <div class="sym">${s.code}</div>
        <div class="nm">${s.name}</div>
        <div class="px">$${fmt(s.last)}</div>
        <div class="row"><span>当日</span><span class="${cls(s.chg1d)}">${arrow(s.chg1d)} ${fmt(s.chg1d)}%</span></div>
        <div class="row"><span>3个月</span><span class="${cls(s.chg3m)}">${fmt(s.chg3m)}%</span></div>
        <div class="row"><span>建议止损</span><span class="dn">$${fmt(sl)} (-${fmt(distSL,1)}%)</span></div>
        <div class="row"><span>量比</span><span class="${s.vol_ratio>1.5?'up':s.vol_ratio<0.7?'dn':'nu'}">${fmt(s.vol_ratio)}×</span></div>
        ${PUBLIC_MODE ? '' : `<div class="pos-panel">
          <div class="pos-row"><span>持仓</span><input type="number" min="0" step="1" value="${p.shares||''}" placeholder="股数" data-code="${s.code}" data-field="shares"></div>
          <div class="pos-row"><span>成本</span><input type="number" min="0" step="0.01" value="${p.cost||''}" placeholder="单价 $" data-code="${s.code}" data-field="cost"></div>
          ${p.shares>0 ? `
          <div class="pos-stat"><span>市值</span><b>$${fmt(mv,0)}</b></div>
          <div class="pos-stat"><span>盈亏</span><b class="${pnl>=0?'up':'dn'}">${pnl>=0?'+':''}$${fmt(pnl,0)} (${fmt(pnlPct)}%)</b></div>
          ` : `<div class="pos-stat" style="text-align:center;color:#475569">未持仓</div>`}
        </div>`}
        ${PUBLIC_MODE ? '' : `<button class="rm" title="从自选移除" onclick="removeStock('${s.code}','${(s.name||'').replace(/'/g,\"\\\\'\")}')">×</button>`}
      </div>`);
  });
  if (!PUBLIC_MODE) {
    cards.querySelectorAll(".pos-panel input").forEach(inp=>{
      inp.addEventListener("change", onPosChange);
      inp.addEventListener("blur", onPosChange);
    });
    renderPortfolio();
  }

  // 明细表
  const tb = document.querySelector("#tbl tbody"); tb.innerHTML = "";
  D.forEach(s=>{
    tb.insertAdjacentHTML("beforeend",`
      <tr>
        <td><b>${s.name}</b> <span style="color:#64748b">${s.code.replace("US.","")}</span></td>
        <td>$${fmt(s.last)}</td>
        <td class="${cls(s.chg1d)}">${fmt(s.chg1d)}%</td>
        <td class="${cls(s.chg1w)}">${fmt(s.chg1w)}%</td>
        <td class="${cls(s.chg1m)}">${fmt(s.chg1m)}%</td>
        <td class="${cls(s.chg3m)}">${fmt(s.chg3m)}%</td>
        <td>${fmt(s.pos_in_range,0)}%</td>
        <td class="${s.vol_ratio>1.5?'up':s.vol_ratio<0.7?'dn':'nu'}">${fmt(s.vol_ratio)}×</td>
        <td>${fmt(s.vol_annual)}%</td>
        <td>${fmt(s.rsi,0)}</td>
        <td>${fmt(s.pe)}</td>
        <td>${fmtB(s.mcap)}</td>
        <td><span class="badge" style="background:${s.color}">${s.signal}</span></td>
      </tr>`);
  });

  // 整体情绪
  const avgScore = D.reduce((a,b)=>a+b.score,0)/D.length;
  const ov = avgScore>=1?"偏多 ▲":avgScore<=-1?"偏空 ▼":"中性 ●";
  const ovColor = avgScore>=1?"#22c55e":avgScore<=-1?"#ef4444":"#94a3b8";
  document.getElementById("overall").innerHTML = `<span style="color:${ovColor};font-weight:600">${ov}</span> (评分 ${avgScore.toFixed(2)})`;

  // 对比柱图
  function barChart(id, vals, color){
    const c = echarts.init(document.getElementById(id),"dark"); charts.push(c);
    c.setOption({
      backgroundColor:"transparent", grid:{left:60,right:20,top:20,bottom:30},
      tooltip:{trigger:"axis"}, xAxis:{type:"category",data:D.map(s=>s.name)},
      yAxis:{type:"value"},
      series:[{type:"bar",data:vals.map((v,i)=>({value:v,itemStyle:{color: typeof color==='function'?color(v,i):color}})), label:{show:true,position:"top",color:"#cbd5e1",fontSize:10,formatter:p=>p.value.toFixed(1)}}]
    });
  }
  barChart("cmp_chg", D.map(s=>s.chg3m), v=> v>=0?"#22c55e":"#ef4444");
  barChart("cmp_vol", D.map(s=>s.vol_annual), "#f59e0b");
  barChart("cmp_vr",  D.map(s=>s.vol_ratio),  v=> v>=1.5?"#22c55e":v<0.7?"#ef4444":"#3b82f6");
  barChart("cmp_pe",  D.map(s=>s.pe),         "#a855f7");

  // K线小图
  const kw = document.getElementById("klines"); kw.innerHTML = "";
  D.forEach((s,i)=>{
    kw.insertAdjacentHTML("beforeend",`<div class="chart-card"><h3>${s.name} (${s.code.replace("US.","")}) · 最新 $${fmt(s.last)} <span class="${cls(s.chg3m)}">${fmt(s.chg3m)}% (3M)</span></h3><div id="k_${i}" class="chart"></div></div>`);
  });
  D.forEach((s,i)=>{
    const c = echarts.init(document.getElementById("k_"+i),"dark"); charts.push(c);
    c.setOption({
      backgroundColor:"transparent",
      legend:{data:["K","MA20","MA50"],top:0,textStyle:{color:"#94a3b8",fontSize:10}},
      grid:[{left:50,right:20,top:30,height:"55%"},{left:50,right:20,top:"72%",height:"20%"}],
      xAxis:[
        {type:"category",data:s.dates,boundaryGap:true,axisLine:{lineStyle:{color:"#334155"}},axisLabel:{color:"#94a3b8",fontSize:9}},
        {type:"category",data:s.dates,gridIndex:1,axisLine:{lineStyle:{color:"#334155"}},axisLabel:{show:false}}
      ],
      yAxis:[
        {scale:true,splitLine:{lineStyle:{color:"#1e293b"}},axisLabel:{color:"#94a3b8",fontSize:9}},
        {gridIndex:1,splitLine:{show:false},axisLine:{show:false},axisLabel:{show:false}}
      ],
      tooltip:{trigger:"axis",axisPointer:{type:"cross"}},
      series:[
        {name:"K",type:"candlestick",data:s.ohlc,itemStyle:{color:"#22c55e",color0:"#ef4444",borderColor:"#22c55e",borderColor0:"#ef4444"}},
        {name:"MA20",type:"line",data:s.ma20_line,smooth:true,symbol:"none",lineStyle:{color:"#f59e0b",width:1}},
        {name:"MA50",type:"line",data:s.ma50_line,smooth:true,symbol:"none",lineStyle:{color:"#a855f7",width:1}},
        {name:"Vol",type:"bar",xAxisIndex:1,yAxisIndex:1,data:s.volumes.map((v,j)=>({value:v,itemStyle:{color: s.ohlc[j][1]>=s.ohlc[j][0]?"#22c55e":"#ef4444"}}))}
      ]
    });
  });

  // 信号解读
  const stb = document.getElementById("sig_tbl"); stb.innerHTML = "";
  D.forEach(s=>{
    const supp = Math.max(s.ma20, s.bb_dn);
    const resi = Math.min(s.hi3m, s.bb_up);
    stb.insertAdjacentHTML("beforeend",`
      <tr>
        <td><b>${s.name}</b></td>
        <td>${s.score>0?'+':''}${s.score}</td>
        <td><span class="badge" style="background:${s.color}">${s.signal}</span></td>
        <td style="text-align:left;font-size:12px;color:#cbd5e1">${s.reasons.join(" · ")}</td>
        <td>$${fmt(supp)} / $${fmt(resi)}</td>
      </tr>`);
  });

  // ----- 分析师定价表 -----
  const recColor = k => ({strong_buy:"#16a34a", buy:"#22c55e", hold:"#94a3b8", underperform:"#f59e0b", sell:"#ef4444"}[k] || "#94a3b8");
  const recText  = k => ({strong_buy:"强烈买入", buy:"买入", hold:"中性", underperform:"减持", sell:"卖出"}[k] || (k||"n/a"));
  const atb = document.querySelector("#ana_tbl tbody"); atb.innerHTML = "";
  D.forEach(s=>{
    const a = s.analyst || {};
    const up = s.upside;
    atb.insertAdjacentHTML("beforeend",`
      <tr>
        <td><b>${s.name}</b></td>
        <td>$${fmt(s.last)}</td>
        <td>${a.tgt_mean?'$'+fmt(a.tgt_mean):'-'}</td>
        <td style="font-size:12px">${a.tgt_low?'$'+fmt(a.tgt_low,0)+' – $'+fmt(a.tgt_high,0):'-'}</td>
        <td class="${up>0?'up':up<0?'dn':'nu'}">${up==null?'-':fmt(up)+'%'}</td>
        <td>${a.n_analyst||'-'}</td>
        <td><span class="badge" style="background:${recColor(a.rec_key)}">${recText(a.rec_key)}</span></td>
        <td>${a.rec_mean?fmt(a.rec_mean):'-'}</td>
        <td>${a.fwd_pe?fmt(a.fwd_pe):'-'}</td>
        <td class="${(a.eps_g||0)>0?'up':(a.eps_g||0)<0?'dn':'nu'}">${a.eps_g==null?'-':fmt(a.eps_g*100,0)+'%'}</td>
        <td class="${(a.rev_g||0)>0?'up':(a.rev_g||0)<0?'dn':'nu'}">${a.rev_g==null?'-':fmt(a.rev_g*100,0)+'%'}</td>
      </tr>`);
  });

  // ----- 定价逻辑解读 -----
  const pb = document.getElementById("pricing_box"); pb.innerHTML = "";
  D.forEach(s=>{
    pb.insertAdjacentHTML("beforeend",`
      <div class="stock-block">
        <h4>${s.name} <span style="color:#64748b">(${s.code.replace("US.","")})</span>
          ${s.upside!=null?`<span class="gauge" style="background:${s.upside>0?'#16a34a':'#ef4444'};color:#fff">分析师隐含上涨空间 ${fmt(s.upside)}%</span>`:''}
        </h4>
        <div class="pricing-card">${s.pricing_logic}</div>
      </div>`);
  });

  // ----- PE 估值对比（当前 PE vs 远期 PE）-----
  const peChart = echarts.init(document.getElementById("cmp_pe2"),"dark"); charts.push(peChart);
  peChart.setOption({
    backgroundColor:"transparent",
    legend:{data:["当前 PE (trailing)","远期 PE (forward)"],top:0,textStyle:{color:"#cbd5e1",fontSize:11}},
    grid:{left:60,right:20,top:36,bottom:30},
    tooltip:{trigger:"axis",formatter:p=>{
      const s = D[p[0].dataIndex];
      const a = s.analyst||{};
      const trend = (a.fwd_pe && a.trail_pe)
        ? (a.fwd_pe < a.trail_pe*0.85 ? "估值消化 📉":
           a.fwd_pe > a.trail_pe*1.05 ? "估值扩张 📈":"估值持平 ➡️")
        : "数据不足";
      return `<b>${s.name}</b><br/>当前 PE: ${a.trail_pe?fmt(a.trail_pe):'-'}<br/>远期 PE: ${a.fwd_pe?fmt(a.fwd_pe):'-'}<br/>趋势: ${trend}`;
    }},
    xAxis:{type:"category",data:D.map(s=>s.name),axisLabel:{color:"#94a3b8",fontSize:11}},
    yAxis:{type:"value",axisLabel:{color:"#94a3b8"}},
    series:[
      {name:"当前 PE (trailing)",type:"bar",data:D.map(s=>s.analyst?.trail_pe||s.pe||0),itemStyle:{color:"#a855f7"},
       label:{show:true,position:"top",color:"#cbd5e1",fontSize:10,formatter:p=>p.value?p.value.toFixed(1):"-"}},
      {name:"远期 PE (forward)",type:"bar",data:D.map(s=>s.analyst?.fwd_pe||0),itemStyle:{color:"#3b82f6"},
       label:{show:true,position:"top",color:"#cbd5e1",fontSize:10,formatter:p=>p.value?p.value.toFixed(1):"-"}}
    ]
  });

  // ----- 个性化交易建议表 (止损/止盈/仓位健康) -----
  const stt = document.getElementById("sltp_tbl"); stt.innerHTML = "";
  D.forEach(s=>{
    const sug = s.sug || {};
    const a = s.analyst || {};
    const p = POS[s.code] || {shares:0, cost:0};
    let health = '<span style="color:#475569">未持仓</span>';
    if(p.shares > 0){
      const pnlPct = p.cost>0 ? ((s.last - p.cost) / p.cost) * 100 : 0;
      const distSL = ((s.last - sug.sl_final) / s.last) * 100;
      if(p.cost > 0 && s.last <= p.cost * 0.93){
        health = `<span class="badge" style="background:#ef4444">🚨 触发 -7% 铁律 ${fmt(pnlPct)}%</span>`;
      } else if(s.last < sug.sl_final){
        health = `<span class="badge" style="background:#ef4444">已破止损位</span>`;
      } else if(distSL < 3){
        health = `<span class="badge" style="background:#f59e0b">⚠️ 接近止损 (${fmt(distSL,1)}%空间)</span>`;
      } else if(p.cost>0 && s.last >= p.cost * 1.5){
        health = `<span class="badge" style="background:#16a34a">✓ +50%, 可减 1/3 (${fmt(pnlPct)}%)</span>`;
      } else if(p.cost>0 && s.last >= p.cost * 1.2){
        health = `<span class="badge" style="background:#22c55e">✓ +20%, 可减 1/3 (${fmt(pnlPct)}%)</span>`;
      } else {
        const c = pnlPct>=0?"#22c55e":"#ef4444";
        health = `<span style="color:${c};font-weight:600">持仓 ${pnlPct>=0?'+':''}${fmt(pnlPct)}%</span>`;
      }
    }
    const tgt = a.tgt_mean ? `$${fmt(a.tgt_mean)}` : '-';
    stt.insertAdjacentHTML("beforeend",`
      <tr>
        <td><b>${s.name}</b> <span style="color:#64748b">${s.code.replace("US.","")}</span></td>
        <td>$${fmt(s.last)}</td>
        <td class="dn">$${fmt(sug.sl_oneil)}</td>
        <td class="dn">$${fmt(sug.sl_atr)}</td>
        <td>$${fmt(sug.sl_ma50)}</td>
        <td><b class="dn">$${fmt(sug.sl_final)}</b> <span style="color:#94a3b8">(${fmt(sug.sl_pct,1)}%)</span></td>
        <td class="up">$${fmt(sug.tp1)}</td>
        <td class="up">$${fmt(sug.tp2)}</td>
        <td>${tgt}</td>
        ${PUBLIC_MODE ? '' : `<td style="text-align:left">${health}</td>`}
      </tr>`);
  });
  if (PUBLIC_MODE) document.querySelectorAll(".pos-col").forEach(el=>el.style.display="none");

  // ----- 板块舆论柱图 -----
  barChart("cmp_sent",   D.map(s=>s.sent_avg), v=> v>0.4?"#22c55e":v<-0.4?"#ef4444":"#94a3b8");
  barChart("cmp_upside", D.map(s=>s.upside||0), v=> v>0?"#22c55e":"#ef4444");

  // ----- 最新舆论 -----
  const nb = document.getElementById("news_box"); nb.innerHTML = "";
  const sentBadge = sc => sc>0?`<span class="sent" style="background:#16a34a;color:#fff">+${sc}</span>`
                          : sc<0?`<span class="sent" style="background:#ef4444;color:#fff">${sc}</span>`
                          : `<span class="sent" style="background:#475569;color:#fff">0</span>`;
  D.forEach(s=>{
    if(!s.news || !s.news.length) return;
    const items = s.news.map(n=>`
      <div class="news-item">
        ${sentBadge(n.sent)}
        <div><a href="${n.url}" target="_blank">${n.title}</a><div class="src">${n.provider} · ${n.pub}</div></div>
        <div style="color:#64748b;font-size:11px;text-align:right">${n.summary?n.summary.slice(0,80)+(n.summary.length>80?'…':''):''}</div>
      </div>`).join("");
    nb.insertAdjacentHTML("beforeend",`
      <div class="stock-block">
        <details ${s.sent_avg!==0?'open':''}>
          <summary><b style="color:#e2e8f0">${s.name}</b> · 情感均值
            <span style="color:${s.sent_avg>0.4?'#22c55e':s.sent_avg<-0.4?'#ef4444':'#94a3b8'};font-weight:600">${s.sent_avg>0?'+':''}${s.sent_avg} ${s.sent_label}</span>
            · ${s.news.length} 条头条
          </summary>
          <div class="news-list">${items}</div>
        </details>
      </div>`);
  });
}

// ----- 持仓输入 / 汇总 -----
let posSaveTimer = null;
function syncPositionsToServer(){
  clearTimeout(posSaveTimer);
  posSaveTimer = setTimeout(async ()=>{
    try{
      await fetch("/api/positions", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({positions: POS})});
    }catch(e){ console.warn("持仓同步服务器失败:", e); }
  }, 500);
}

function onPosChange(e){
  const inp = e.target;
  const code = inp.dataset.code;
  const field = inp.dataset.field;
  const v = parseFloat(inp.value);
  POS[code] = POS[code] || {shares:0, cost:0};
  POS[code][field] = isNaN(v) ? 0 : v;
  // 如果股数为 0 且成本为 0，则删除该条目
  if((POS[code].shares||0) <= 0 && (POS[code].cost||0) <= 0) delete POS[code];
  persistLocal();
  syncPositionsToServer();
  // 仅局部刷新汇总和该卡片显示
  renderPortfolio();
  updateCardPnl(code);
}

function updateCardPnl(code){
  const s = D.find(x=>x.code===code); if(!s) return;
  const p = POS[code] || {shares:0, cost:0};
  // 找到所属 card：用 input 的 closest .card
  const inp = document.querySelector(`.pos-panel input[data-code="${code}"]`);
  if(!inp) return;
  const panel = inp.closest(".pos-panel");
  // 清掉旧的 pos-stat
  panel.querySelectorAll(".pos-stat").forEach(el=>el.remove());
  if(p.shares > 0){
    const mv = p.shares * s.last;
    const cb = p.shares * (p.cost || 0);
    const pnl = mv - cb;
    const pnlPct = cb>0 ? (pnl/cb)*100 : 0;
    panel.insertAdjacentHTML("beforeend",
      `<div class="pos-stat"><span>市值</span><b>$${fmt(mv,0)}</b></div>
       <div class="pos-stat"><span>盈亏</span><b class="${pnl>=0?'up':'dn'}">${pnl>=0?'+':''}$${fmt(pnl,0)} (${fmt(pnlPct)}%)</b></div>`);
  } else {
    panel.insertAdjacentHTML("beforeend", `<div class="pos-stat" style="text-align:center;color:#475569">未持仓</div>`);
  }
}

function renderPortfolio(){
  const box = document.getElementById("portfolio_box");
  let totalCost = 0, totalMv = 0, count = 0, winners = 0;
  const breakdown = [];
  D.forEach(s=>{
    const p = POS[s.code];
    if(!p || !p.shares) return;
    const mv = p.shares * s.last;
    const cb = p.shares * (p.cost || 0);
    totalMv += mv;
    totalCost += cb;
    count += 1;
    if(mv >= cb) winners += 1;
    breakdown.push({name:s.name, code:s.code, mv, cb, pnl: mv - cb});
  });
  if(count === 0){
    box.innerHTML = `<div class="item" style="grid-column:1/-1;color:#64748b;font-size:13px">尚未填写任何持仓 · 在下方卡片的"持仓/成本"输入框填入即可</div>`;
    return;
  }
  const pnl = totalMv - totalCost;
  const pnlPct = totalCost>0 ? (pnl/totalCost)*100 : 0;
  // 集中度：最大单仓占比
  const maxWeight = Math.max(...breakdown.map(b=>b.mv))/totalMv*100;
  const concentrationColor = maxWeight > 25 ? "#ef4444" : maxWeight > 15 ? "#f59e0b" : "#22c55e";
  box.innerHTML = `
    <div class="item"><div class="label">持仓数</div><div class="val">${count}</div></div>
    <div class="item"><div class="label">总成本</div><div class="val">$${fmt(totalCost,0)}</div></div>
    <div class="item"><div class="label">总市值</div><div class="val">$${fmt(totalMv,0)}</div></div>
    <div class="item"><div class="label">浮动盈亏</div><div class="val ${pnl>=0?'up':'dn'}">${pnl>=0?'+':''}$${fmt(pnl,0)}</div></div>
    <div class="item"><div class="label">盈亏%</div><div class="val ${pnl>=0?'up':'dn'}">${pnl>=0?'+':''}${fmt(pnlPct)}%</div></div>
    <div class="item"><div class="label">胜率</div><div class="val">${winners}/${count}</div></div>
    <div class="item"><div class="label">最大单仓占比</div><div class="val" style="color:${concentrationColor}">${fmt(maxWeight,1)}%${maxWeight>10?' ⚠':''}</div></div>
  `;
}

// ----- 手动推 GitHub Pages 快照 -----
async function refreshSnapState(){
  try{
    const r = await fetch("/api/snapshot/status", {cache:"no-store"});
    const j = await r.json();
    const el = document.getElementById("snap_state");
    if(!el) return;
    if(j.running){ el.innerHTML = '<span style="color:#fbbf24">推送中…</span>'; return; }
    if(j.last_err){ el.innerHTML = `<span style="color:#ef4444">上次失败: ${j.last_err.slice(0,60)}</span>`; return; }
    if(j.last_ts){
      el.innerHTML = `上次快照 <b>${j.last_ts}</b> · <span style="color:#94a3b8">${j.last_git}</span> · <a href="${j.pages_url}" target="_blank" style="color:#60a5fa">公开页 →</a>`;
    } else {
      el.innerHTML = '<span style="color:#64748b">从未推送 (点 📸 推快照)</span>';
    }
  }catch(e){}
}
async function pushSnapshot(){
  const btn = document.getElementById("snapBtn");
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span><span>推送中…</span>';
  try{
    const r = await fetch("/api/snapshot", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    const j = await r.json();
    if(!r.ok || j.error) throw new Error(j.error || ("HTTP "+r.status));
    alert(`✅ 快照已推 ${j.git}\n时间: ${j.ts}\n大小: ${j.size_kb} KB\n\n公开页 1 分钟内更新:\n${j.pages_url}`);
  }catch(e){
    alert("推送失败: "+e.message);
  }finally{
    btn.disabled = false;
    btn.innerHTML = orig;
    refreshSnapState();
  }
}

async function refresh(){
  const btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span><span>拉取中...</span>';
  try{
    const r = await fetch("/api/data", {cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const j = await r.json();
    if(j.error) throw new Error(j.error);
    D = j.data; WL = j.watchlist || WL;
    lastUpdate = Date.now();
    document.getElementById("ts").textContent = j.ts;
    render();
    updateAgo();
  }catch(e){
    alert("刷新失败: "+e.message+"\\n请确认 Python 服务和 FutuOpenD 仍在运行。");
  }finally{
    btn.disabled = false;
    btn.innerHTML = '<span>⟳</span><span>刷新</span>';
  }
}

// ----- 自选股 增/删 -----
async function addStock(code, name){
  hideSearch();
  document.getElementById("searchInput").value = "";
  try{
    const r = await fetch("/api/watchlist/add", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({code, name})});
    const j = await r.json();
    if(!r.ok){ alert(j.error || "添加失败"); return; }
    await refresh();
  }catch(e){ alert("添加失败: "+e.message); }
}

async function removeStock(code, name){
  if(!confirm("从自选移除 "+(name||code)+" ?")) return;
  try{
    const r = await fetch("/api/watchlist/remove", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({code})});
    const j = await r.json();
    if(!r.ok){ alert(j.error || "移除失败"); return; }
    await refresh();
  }catch(e){ alert("移除失败: "+e.message); }
}

async function resetWatchlist(){
  if(!confirm("恢复为默认 Mag7 自选 (会清空当前自选)?")) return;
  try{
    const r = await fetch("/api/watchlist/reset", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    if(!r.ok){ const j = await r.json(); alert(j.error || "失败"); return; }
    await refresh();
  }catch(e){ alert("失败: "+e.message); }
}

// ----- 搜索 -----
let searchTimer = null;
const sInput = document.getElementById("searchInput");
const sResults = document.getElementById("searchResults");

function hideSearch(){ sResults.classList.remove("show"); }

sInput.addEventListener("input", ()=>{
  clearTimeout(searchTimer);
  const q = sInput.value.trim();
  if(q.length < 1){ hideSearch(); return; }
  searchTimer = setTimeout(async ()=>{
    try{
      const r = await fetch("/api/search?q="+encodeURIComponent(q));
      const j = await r.json();
      const inWL = new Set(WL.map(x=>x.code));
      if(!j.results || !j.results.length){
        sResults.innerHTML = '<div class="sr-empty">未找到匹配的美股</div>';
      } else {
        sResults.innerHTML = j.results.map(it=>{
          const exists = inWL.has(it.code);
          const codeShort = it.code.replace("US.","");
          const safeName = (it.name||"").replace(/'/g,"&#39;").replace(/"/g,"&quot;");
          return `<div class="sr-item" ${exists?'style="opacity:.5;cursor:not-allowed" title="已在自选"':`onclick="addStock('${it.code}','${safeName.replace(/'/g,"\\\\'")}')"`}>
            <span class="sr-code">${codeShort}</span>
            <span class="sr-name">${safeName}</span>
            <span class="sr-add">${exists?'✓':'+'}</span>
          </div>`;
        }).join("");
      }
      sResults.classList.add("show");
    }catch(e){
      sResults.innerHTML = '<div class="sr-empty">搜索失败: '+e.message+'</div>';
      sResults.classList.add("show");
    }
  }, 200);
});
sInput.addEventListener("focus", ()=>{ if(sInput.value.trim()) sResults.classList.add("show"); });
document.addEventListener("click", e=>{
  if(!e.target.closest(".search-wrap")) hideSearch();
});

function updateAgo(){
  const sec = Math.floor((Date.now()-lastUpdate)/1000);
  const txt = sec<5?"刚刚": sec<60?`${sec}秒前`: sec<3600?`${Math.floor(sec/60)}分${sec%60}秒前`: `${Math.floor(sec/3600)}小时前`;
  document.getElementById("ago").textContent = txt;
}

render();
setInterval(updateAgo, 1000);
window.addEventListener("resize", ()=> charts.forEach(c=>c.resize()));
if (!PUBLIC_MODE) refreshSnapState();
</script>
</body></html>
"""

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

class Handler(BaseHTTPRequestHandler):
    def _deny(self, why):
        body = f"Forbidden: {why}".encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"[block] {why} from {self.client_address[0]} on {self.command} {self.path}")
        return False

    def _local_only(self):
        # Host 必须是 loopback，防 DNS rebinding（攻击者把 evil.com 解析到 127.0.0.1）
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in LOCAL_HOSTS:
            return self._deny(f"非法 Host: {host or '(空)'}")
        # 写操作必须带本地 Origin/Referer，防止浏览器跨站 fetch
        if self.command in ("POST", "PUT", "DELETE", "PATCH"):
            from urllib.parse import urlparse
            ref = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if not ref:
                return self._deny("缺少 Origin/Referer (CSRF 防护)")
            if (urlparse(ref).hostname or "") not in LOCAL_HOSTS:
                return self._deny(f"非法 Origin/Referer: {ref}")
        return True

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._local_only(): return
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path == "/" or u.path.startswith("/index"):
            print("[GET /] 首次加载，拉取数据...")
            data, ts = build_payload()
            wl = load_watchlist()
            payload = json.dumps({"data": data, "ts": ts,
                                  "watchlist": [{"code": c, "name": n} for c, n in wl],
                                  "positions": load_positions()}, ensure_ascii=False)
            html = HTML_TMPL.replace("__BOOTSTRAP__", payload)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/data":
            print("[GET /api/data] 刷新中...")
            try:
                data, ts = build_payload()
                wl = load_watchlist()
                self._json({"data": data, "ts": ts,
                            "watchlist": [{"code": c, "name": n} for c, n in wl],
                            "positions": load_positions()})
            except Exception as e:
                self._json({"error": str(e)}, status=500)
        elif u.path == "/api/search":
            q = (parse_qs(u.query).get("q", [""])[0] or "").strip()
            try:
                self._json({"results": search_stocks(q, limit=20)})
            except Exception as e:
                self._json({"error": str(e)}, status=500)
        elif u.path == "/api/watchlist":
            self._json({"watchlist": [{"code": c, "name": n} for c, n in load_watchlist()]})
        elif u.path == "/api/positions":
            self._json({"positions": load_positions()})
        elif u.path == "/api/snapshot/status":
            self._json({"running": _SNAPSHOT["running"], "last_ts": _SNAPSHOT["last_ts"],
                        "last_git": _SNAPSHOT["last_git"], "last_err": _SNAPSHOT["last_err"],
                        "pages_url": PAGES_URL})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._local_only(): return
        from urllib.parse import urlparse
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": "无法解析请求体"}, status=400)

        if u.path == "/api/watchlist/add":
            code, name = body.get("code"), body.get("name", "")
            if not code: return self._json({"error": "缺少 code"}, status=400)
            wl = load_watchlist()
            if any(c == code for c, _ in wl):
                return self._json({"error": "已在自选中", "watchlist": [{"code": c, "name": n} for c, n in wl]}, status=409)
            if len(wl) >= MAX_WATCHLIST:
                return self._json({"error": f"自选最多 {MAX_WATCHLIST} 只"}, status=400)
            wl.append((code, name or code))
            save_watchlist(wl)
            _YF_CACHE["ts"] = 0  # 失效 yfinance 缓存以纳入新股
            print(f"[watchlist] + {code} ({name}) → 共 {len(wl)} 只")
            self._json({"ok": True, "watchlist": [{"code": c, "name": n} for c, n in wl]})
        elif u.path == "/api/watchlist/remove":
            code = body.get("code")
            wl = [(c, n) for c, n in load_watchlist() if c != code]
            if len(wl) == 0:
                return self._json({"error": "自选不能为空"}, status=400)
            save_watchlist(wl)
            print(f"[watchlist] - {code} → 共 {len(wl)} 只")
            self._json({"ok": True, "watchlist": [{"code": c, "name": n} for c, n in wl]})
        elif u.path == "/api/positions":
            pos = body.get("positions") or {}
            # 净化：仅保留 shares>0 的项
            clean = {k: {"shares": float(v.get("shares") or 0), "cost": float(v.get("cost") or 0)}
                     for k, v in pos.items() if v and (v.get("shares") or 0)}
            save_positions(clean)
            print(f"[positions] saved {len(clean)} holdings")
            return self._json({"ok": True, "positions": clean})
        elif u.path == "/api/snapshot":
            # 单任务串行 (避免并发 push 冲突)
            acquired = _SNAPSHOT["lock"].acquire(blocking=False)
            if not acquired or _SNAPSHOT["running"]:
                if acquired: _SNAPSHOT["lock"].release()
                return self._json({"error": "已有快照任务在跑, 请稍等"}, status=429)
            _SNAPSHOT["running"] = True
            try:
                r = run_snapshot()
                _SNAPSHOT["last_ts"] = r["ts"]
                _SNAPSHOT["last_git"] = r["git"]
                _SNAPSHOT["last_err"] = None
                self._json({"ok": True, **r})
            except Exception as e:
                _SNAPSHOT["last_err"] = str(e)
                self._json({"error": str(e)}, status=500)
            finally:
                _SNAPSHOT["running"] = False
                _SNAPSHOT["lock"].release()
        elif u.path == "/api/watchlist/reset":
            save_watchlist(DEFAULT_WATCHLIST)
            _YF_CACHE["ts"] = 0
            print("[watchlist] reset 为默认 Mag7")
            self._json({"ok": True, "watchlist": [{"code": c, "name": n} for c, n in DEFAULT_WATCHLIST]})
        else:
            self.send_error(404)

    def log_message(self, *a, **k):  # 静默默认日志
        return

def main():
    bind = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
    if bind not in LOCAL_HOSTS:
        raise SystemExit(f"❌ 服务无鉴权，禁止绑定 {bind}；如需公网暴露请先恢复 Basic Auth")
    url = f"http://{bind}:{SERVE_PORT}/"
    srv = ThreadingHTTPServer((bind, SERVE_PORT), Handler)
    srv.daemon_threads = True
    print(f"服务启动: {url}  (Ctrl+C 退出)")
    if not os.environ.get("DASHBOARD_NO_BROWSER"):
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.server_close()

if __name__ == "__main__":
    main()
