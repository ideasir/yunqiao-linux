#!/usr/bin/env bash
# ============================================================
#  云桥 Linux 客户端 一键部署
#  一条命令：
#    curl -fsSL https://raw.githubusercontent.com/ideasir/yunqiao/main/linux/install.sh | bash
#  部署完成后打开返回的 WebUI 地址 → 扫码绑定 → 配置中转 → 连接
# ============================================================
set -euo pipefail

WEBUI_PORT="${WEBUI_PORT:-8080}"
BRANCH="${BRANCH:-main}"
# 多源下载（独立公开仓库，匿名可访问）
API_BASE="https://api.github.com/repos/ideasir/yunqiao-linux/contents"
CDN_BASE="https://cdn.jsdelivr.net/gh/ideasir/yunqiao-linux@$BRANCH"
RAW_BASE="https://raw.githubusercontent.com/ideasir/yunqiao-linux/$BRANCH"

# 端口自适应：默认 WEBUI_PORT，被占用则自动找空闲端口
port_free() { ! ss -tln 2>/dev/null | grep -q ":$1 "; }
pick_port() {
  local p="$1"
  local i
  for i in $(seq 0 30); do
    port_free "$((p + i))" && { echo "$((p + i))"; return; }
  done
  echo "$p"  # 兜底
}

# 带多源 fallback 的下载：$1=文件名 $2=目标路径
fetch() {
  curl -fsSL -m 20 -H "Accept: application/vnd.github.raw+json" "$API_BASE/$1" -o "$2" 2>/dev/null && return 0
  curl -fsSL -m 20 "$CDN_BASE/$1" -o "$2" 2>/dev/null && return 0
  curl -fsSL -m 20 "$RAW_BASE/$1" -o "$2"
}

WORKER_DIR="${YUNQIAO_WORKER_DIR:-$HOME/yunqiao}"
WEBUI_DIR="${YUNQIAO_WEBUI_DIR:-$HOME/webui}"
CONFIG_DIR="${YUNQIAO_CONFIG_DIR:-$HOME/.yunqiao}"

log()  { printf '\033[1;36m[云桥]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[错误]\033[0m %s\n' "$*"; exit 1; }

log "开始部署云桥 Linux 客户端 ..."

# ─── 0. root 提醒 ────────────────────────────────
[ "$(id -u)" -eq 0 ] && warn "当前以 root 运行，worker 会以 root 权限执行命令，请谨慎"

# ─── 1. 安装依赖 ──────────────────────────────────
log "检查/安装依赖 ..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq python3 python3-websockets python3-psutil curl >/dev/null 2>&1 || true
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q python3 python3-websockets python3-psutil curl >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-websockets python3-psutil curl >/dev/null 2>&1 || true
else
  warn "未识别包管理器，请手动安装 python3 / websockets / psutil"
fi
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | tr -dc '0-9' | cut -c1-2)" -lt 20 ]; then
  log "安装 Node.js 20 ..."
  if command -v apt-get >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 || true
    apt-get install -y -qq nodejs >/dev/null 2>&1 || true
  else
    warn "请手动安装 Node.js 20+（https://nodejs.org）"
  fi
fi
command -v node >/dev/null 2>&1 || die "Node.js 未安装成功"

# ─── 2. 拉取代码 ──────────────────────────────────
log "下载 Worker → $WORKER_DIR"
mkdir -p "$WORKER_DIR" "$WORKER_DIR/worker"
for f in tcp_agent.py common.py; do
  fetch "$f" "$WORKER_DIR/$f" || die "下载 $f 失败"
done

log "下载 WebUI → $WEBUI_DIR"
mkdir -p "$WEBUI_DIR"
for f in webui-server.js ui.html qrcode.min.js; do
  fetch "$f" "$WEBUI_DIR/$f" || die "下载 $f 失败"
done

# ─── 3. 初始化配置（中转在 WebUI 里配置）─────────
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.json" ]; then
  cat > "$CONFIG_DIR/config.json" <<EOF
{
  "relayUrl": "",
  "key": "",
  "workDir": "$WORKER_DIR/worker",
  "deviceName": "$(hostname)"
}
EOF
  log "已生成配置 $CONFIG_DIR/config.json（中转信息待 WebUI 里填写）"
fi

# ─── 4. 启动 WebUI（端口自适应）────────────────
WEBUI_PORT=$(pick_port "$WEBUI_PORT")
log "启动 WebUI（端口 $WEBUI_PORT）..."
pkill -f "[n]ode webui-server.js" 2>/dev/null || true
sleep 1
cd "$WEBUI_DIR"
YUNQIAO_WEBUI_PORT="$WEBUI_PORT" setsid nohup node webui-server.js > webui.log 2>&1 < /dev/null &
sleep 3
if pgrep -f "[n]ode webui-server.js" >/dev/null; then
  log "✅ WebUI 已启动（端口 $WEBUI_PORT）"
else
  warn "WebUI 启动失败，请看 $WEBUI_DIR/webui.log"
  tail -5 "$WEBUI_DIR/webui.log" 2>/dev/null || true
  exit 1
fi

# ─── 5. 获取本机地址并输出 ────────────────────────
IPV4="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IPV4" ] && IPV4="127.0.0.1"
PUBLIC_IP="$(curl -fsSL -m 5 https://api.ipify.org 2>/dev/null || echo '')"

echo ""
log "════════════════ 部署完成 ════════════════"
log "  🌐 WebUI 地址:  http://$IPV4:$WEBUI_PORT"
[ -n "$PUBLIC_IP" ] && log "  🌐 公网地址:    http://$PUBLIC_IP:$WEBUI_PORT  （需云安全组放行端口）"
log "  ────────────────────────────────────────"
log "  接下来（在浏览器里）:"
log "  1. 打开上面的 WebUI 地址"
log "  2. 用手机验证器 App 扫码绑定（仅首次）"
log "  3. 进入后在【配置】里填入中转服务器地址与密钥"
log "  4. 点【连接】→ Worker 自动连上中转，Agent 即可远程操作这台机器"
log "  ────────────────────────────────────────"
log "  日志: $WEBUI_DIR/webui.log | $WORKER_DIR/agent.log"
log "══════════════════════════════════════════"