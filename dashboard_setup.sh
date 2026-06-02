#!/bin/bash
# Mag7 Dashboard 公网部署一键设置脚本
# 用 Tailscale Funnel 暴露本地 8765 端口为固定 HTTPS 公网域名
set -e
cd "$(dirname "$0")"

GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; CYAN="\033[36m"; NC="\033[0m"
step() { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }
ask()  { read -p "$(echo -e ${YELLOW}?${NC} $1) " r; echo "$r"; }

step "1/6  检查 Python 依赖"
python3 -c "import futu, yfinance, curl_cffi, pandas" 2>/dev/null \
  && ok "依赖齐全" \
  || { err "缺依赖, 跑: pip3 install --user futu-api yfinance curl_cffi pandas numpy"; exit 1; }

step "2/6  检查 / 安装 Tailscale"
if [ -d /Applications/Tailscale.app ]; then
  ok "Tailscale.app 已安装"
else
  warn "未检测到 Tailscale.app"
  echo "请用以下任一方式安装:"
  echo "  · App Store 搜 'Tailscale' (推荐, 含 GUI)"
  echo "  · 命令行 brew: brew install --cask tailscale"
  read -p "安装完成后按回车继续..."
fi
# 找 tailscale CLI 路径 (App 版藏在 .app 里)
TS=$(command -v tailscale || true)
[ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] \
  && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ -z "$TS" ] && { err "找不到 tailscale 命令"; exit 1; }
ok "tailscale CLI: $TS"

step "3/6  Tailscale 登录 + 启动"
if $TS status >/dev/null 2>&1; then
  ok "Tailscale 已登录: $($TS status --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["Self"]["DNSName"].rstrip("."))')"
else
  warn "未登录, 启动登录流程 (浏览器会自动打开)..."
  $TS up
fi

step "4/6  启用 HTTPS + Funnel (一次性, 需登录 Tailscale 管理后台)"
cat <<EOF
请在浏览器打开 ${CYAN}https://login.tailscale.com/admin/dns${NC} 并:
  1. DNS 页 → MagicDNS 已开 → 找到 ${CYAN}HTTPS Certificates${NC} → 点 Enable HTTPS
  2. 打开 ${CYAN}https://login.tailscale.com/admin/settings/funnel${NC} → 把本机加入 Funnel 允许列表
     (或在 ACL 里加 "nodeAttrs": [{"target":["*"], "attr":["funnel"]}])
EOF
read -p "上面两步都点完之后按回车继续..."

step "5/6  注册 Python 看板为 launchd 后台服务"
PLIST=~/Library/LaunchAgents/com.user.mag7-dashboard.plist
[ -f "$PLIST" ] || { err "$PLIST 不存在, 先生成"; exit 1; }
# 卸了旧的再装, 避免 'already loaded'
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
sleep 3
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/ | grep -q "401"; then
  ok "看板服务已起 (本地 127.0.0.1:8765 返回 401 = 鉴权生效)"
else
  err "看板服务未正常响应, 看日志: tail -f ~/Library/Logs/mag7-dashboard.err.log"
  exit 1
fi
ok "账号文件: ~/.dashboard_auth.json"
echo "  当前账号:"
python3 -c "import json; d=json.load(open('/Users/admin/.dashboard_auth.json'))['users']; [print(f'    · {u} / {p}') for u,p in d.items()]"

step "6/6  开 Tailscale Funnel → 8765"
$TS funnel --bg 8765 || { err "funnel 失败, 检查第 4 步是否完成"; exit 1; }
sleep 1
URL=$($TS funnel status 2>/dev/null | /usr/bin/grep -E 'https://' | head -1 | awk '{print $1}')
[ -z "$URL" ] && URL="https://$($TS status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

echo
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
ok "部署完成 🎉"
echo
echo -e "  ${CYAN}公网 URL${NC}: $URL"
echo -e "  ${CYAN}账号管理${NC}: 编辑 ~/.dashboard_auth.json 添加用户 (改完无需重启, 自动重读)"
echo -e "  ${CYAN}查看日志${NC}: tail -f ~/Library/Logs/mag7-dashboard.log"
echo -e "  ${CYAN}代码目录${NC}: /Users/admin/mag7-dashboard/"
echo -e "  ${CYAN}停止服务${NC}: launchctl unload ~/Library/LaunchAgents/com.user.mag7-dashboard.plist"
echo -e "  ${CYAN}关闭外网${NC}: $TS funnel off"
echo
warn "提醒: FutuOpenD 也需要保持运行 (开机自启请在 Futu 软件里设置)"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
