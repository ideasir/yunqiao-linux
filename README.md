# 云桥 Linux 客户端

在 Linux 上部署云桥客户端，让 **沙箱/上游 Agent 通过中转服务器操作这台 Linux**，并提供一个带 **TOTP 手机验证器认证** 的 WebUI 管理界面。

## 📁 组成

| 文件 | 作用 |
|------|------|
| `tcp_agent.py` + `common.py` | **Worker**：反向连接中转服务器，接收并执行 Agent 命令（纯 Python 跨平台） |
| `webui-server.js` | **WebUI 后端**（Node 零依赖）：状态/会话/消息/命令/文件 + TOTP 认证 + "网页开=连接、关=断开" |
| `ui.html` | WebUI 前端（复用 Windows 桌面版界面，运行时注入桥接 shim） |
| `qrcode.min.js` | TOTP 绑定二维码库 |
| `systemd/` | 开机自启服务文件（可选） |

---

## 🚀 从零部署（完整步骤）

### 第 1 步：环境准备

**Ubuntu/Debian 系统：**

```bash
# Python 3.8+ 与 worker 依赖
sudo apt-get update
sudo apt-get install -y python3 python3-websockets python3-psutil

# Node.js 20+（WebUI 需要）。若没有 Node，先装：
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version   # 应输出 v20.x+
```

**CentOS/RHEL：**

```bash
sudo yum install -y python3 python3-websockets python3-psutil
# Node 20+ 从 https://nodejs.org 下载二进制包安装
```

### 第 2 步：获取代码

```bash
cd ~
git clone https://github.com/ideasir/yunqiao.git
cd yunqiao/linux
```

### 第 3 步：部署文件

```bash
# worker 目录
mkdir -p ~/yunqiao
cp tcp_agent.py common.py ~/yunqiao/

# WebUI 目录
mkdir -p ~/webui
cp webui-server.js ui.html qrcode.min.js ~/webui/
```

### 第 4 步：启动 worker（连接中转服务器）

把 `<RELAY_HOST>` 换成你的中转服务器地址（如 `yunqiao.very.im`）：

```bash
cd ~/yunqiao
setsid nohup python3 tcp_agent.py \
  --reverse --relay-ip <RELAY_HOST> --relay-port 19998 \
  > agent.log 2>&1 < /dev/null &

# 验证：应看到连接成功 + 心跳
tail -5 agent.log
# 期望: [tcp-agent] 已连接到中继服务器 ... / relay_status connected
```

> **worker 无需配对码**——设备自动注册到中转；Agent 侧用中转的 MCP 地址 + 配对码调用。

### 第 5 步：启动 WebUI（TOTP 认证）

```bash
cd ~/webui
setsid nohup node webui-server.js > webui.log 2>&1 < /dev/null &

# 验证
tail -3 webui.log   # 应显示 云桥 Linux WebUI: http://0.0.0.0:8080（TOTP 认证）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login   # 200
```

**浏览器访问**：`http://<服务器IP>:8080`

| 场景 | 操作 |
|------|------|
| **首次访问** | 显示二维码 → 手机装验证器（Google/微软/Authy）→ 扫码绑定 |
| **绑定后** | 二维码永久隐藏，登录页只剩验证码输入框 |
| **以后登录** | 打开页面 → 输入 App 的 6 位动态码 → 进入 |

> 若浏览器无法打开 `http://IP:8080`，检查云服务器安全组/防火墙是否放行 8080 端口。

### 第 6 步：配置中转连接参数（可选）

WebUI 连接中转服务器的地址与管理员密钥在【WebUI 配置】里填写（保存后自动生效，密钥显示为脱敏格式）。

如需预置配置（部署时初始化）：
1. 先创建配置文件：
   ```bash
   mkdir -p ~/.yunqiao
   echo '{"relayUrl":"https://<你的中转>","key":"<管理员密钥>"}' > ~/.yunqiao/config.json
   ```
2. 重启 WebUI。

---

## 🌐 域名 + HTTPS 反代（推荐）

WebUI 默认监听 `0.0.0.0:8081`，可通过域名 + Nginx 反代提供 HTTPS 访问（避免 IP+端口被网络限制）。

仓库 `linux/nginx/` 提供完整配置示例：

```bash
# 1. 签证书（acme.sh）
curl https://get.acme.sh | sh
~/.acme.sh/acme.sh --issue -d <你的域名> --webroot /var/www/certbot

# 2. 用 nginx 示例改域名/证书路径后放入 nginx
sudo cp linux/nginx/arm.oauth.eu.cc.conf /etc/nginx/sites-enabled/<你的域名>
sudo nginx -t && sudo systemctl reload nginx
```

> ⚠️ **关键坑（必须配置）**：`proxy_buffering off;`——WebUI 用 **SSE** 实时推送中转状态，nginx 默认缓冲会把推送卡住，界面永远显示"连不上中转服务器"，示例已包含，别删掉。

## ⚙️ systemd 常驻（可选，两种模式）

### 模式 A：worker 常驻（随时可被 Agent 操作，不受网页开关控制）

```bash
sudo cp ~/yunqiao/../linux/systemd/yunqiao-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yunqiao-worker
```

### 模式 B：WebUI 常驻（网页开连关断语义）

```bash
sudo cp ~/yunqiao/../linux/systemd/yunqiao-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yunqiao-webui
```

> ⚠️ **两者不要同时用于 worker**：模式 A 会让 worker 常驻；模式 B 下 worker 由 WebUI 按"网页开关"拉启/停止。用哪种选一种即可（默认推荐 B，安全）。

---

## 🖥️ WebUI 功能

- **TOTP 登录**：首次扫码绑定，绑定后二维码永久隐藏（防他人扫码进入）
- **网页会话 = 连接开关**：打开页面自动连接中转，全部关闭 8 秒后自动断开
- 状态卡片（CPU/内存/负载/worker 状态）、会话管理、消息收发（relay）、配对码、命令执行、文件浏览、codegraph
- 配置存 `~/.yunqiao/config.json`（TOTP secret 仅 root 可读）

## 🔧 常见问题

| 问题 | 解决 |
|------|------|
| worker 连不上 | 检查 `--relay-ip` 是否正确、中转 19998 端口是否开放、`agent.log` 报错 |
| WebUI 打不开 | 安全组/防火墙放行 8080；`webui.log` 查端口占用 |
| 登录提示验证码错误 | 手机时间要校准（TOTP 依赖时间，±90 秒窗口） |
| 想改端口 | 启动前 `export YUNQIAO_WEBUI_PORT=<端口>` 或用 `YUNQIAO_WEBUI_PORT` 环境变量 |
| worker 常驻却想断连 | 用 `systemctl stop yunqiao-worker`，或切到模式 B |

## 与 Windows 客户端的差异

- `tcp_agent.py` 适配 Linux：`python3` 解释器、`pgrep` 进程管理、bash 语言
- `start_process` 日志参数用 **`redirectLog`**
- WebUI 由后端直接提供（Windows 桌面版是 pywebview 壳）