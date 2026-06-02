# Mag7 / 自选股分析看板

基于 FutuOpenD + Yahoo Finance 的本地实时股票看板，含技术指标、分析师定价、舆论情感、止损止盈建议、持仓追踪。

## 两种部署形态

| 形态 | 数据 | 鉴权 | 公网访问 |
|---|---|---|---|
| **本地实时服务** (`mag7_dashboard.py`) | 实时拉取 | Basic Auth 多用户 | Tailscale Funnel |
| **静态快照** (`docs/index.html`) | 每小时刷新一次 | GitHub Pages 公开 | 直接发 URL，无需鉴权 |

静态快照**永远隐藏个人持仓**（金额/股数/盈亏），只展示技术分析。

## 本地启动

```bash
python3 mag7_dashboard.py
# 首次运行会生成 ~/.dashboard_auth.json (默认 admin 账号)
# 浏览器打开 http://127.0.0.1:8765/
```

## 后台常驻 (launchd)

```bash
launchctl load ~/Library/LaunchAgents/com.user.mag7-dashboard.plist
tail -f ~/Library/Logs/mag7-dashboard.log
```

## 公网 (Tailscale Funnel)

```bash
bash dashboard_setup.sh
# 拿到固定 https://<machine>.tail-XXXX.ts.net
```

## 生成静态快照 (公开版)

```bash
python3 render_static.py --push
# 写入 docs/index.html + git push (GitHub Pages 5 分钟内更新)
```

## 文件结构

```
mag7-dashboard/
├── mag7_dashboard.py          # 实时服务主程序
├── render_static.py           # 静态快照生成器
├── dashboard_setup.sh         # Tailscale Funnel 一键部署
├── docs/index.html            # GitHub Pages 入口 (自动生成)
└── .github/workflows/         # CI (可选)
```

私密文件 (永不提交)：
- `~/.dashboard_auth.json` — 用户名密码
- `~/.positions_dashboard.json` — 持仓
- `~/.watchlist_dashboard.json` — 自选
- `~/.us_stock_universe.json` — Futu 美股代码缓存

## 安全

- Basic Auth 多用户 + 401 失败日志
- 静态快照零暴露持仓数据
- 写 API (`/api/watchlist`, `/api/positions`) 必须先通过鉴权
