#!/usr/bin/env node
/**
 * 云桥 Linux WebUI 服务端（TOTP 手机验证器认证版）
 * 复用 ui.html（注入 pywebview 桥接 shim）→ 浏览器访问
 * 认证：Google Authenticator / 任何 TOTP 验证器（扫码绑定 → 6 位动态码登录）
 * 端口：1777
 */
'use strict';

const http = require('http');
const https = require('https');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { exec } = require('child_process');

// ─── 配置 ────────────────────────────────────────
const CONFIG_DIR = process.env.YUNQIAO_CONFIG || path.join(os.homedir(), '.yunqiao');
const CONFIG_FILE = path.join(CONFIG_DIR, 'config.json');
const SESSIONS_FILE = path.join(CONFIG_DIR, 'sessions.json');
const UI_PATH = process.env.YUNQIAO_UI || path.join(__dirname, 'ui.html');
const QR_PATH = path.join(__dirname, 'qrcode.min.js');
const AGENT_DIR = path.join(os.homedir(), 'yunqiao');
const AGENT_LOG = path.join(AGENT_DIR, 'agent.log');
const PORT = parseInt(process.env.YUNQIAO_WEBUI_PORT || '8080', 10);

function loadJson(p, fb) { try { return JSON.parse(fs.readFileSync(p, 'utf-8')); } catch (e) { return fb; } }
function saveJson(p, o) { try { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, JSON.stringify(o, null, 2), 'utf-8'); return true; } catch (e) { return false; } }

const cfg = loadJson(CONFIG_FILE, {});
let RELAY_URL = (cfg.relayUrl || 'https://yunqiao.very.im').replace(/\/+$/, '');
let RELAY_KEY = cfg.key || 'eman821015';
let pairCode = cfg.pairCode || String(Math.floor(100000 + Math.random() * 900000));
let mcpTicket = null;

// ─── TOTP 认证 ──────────────────────────────────
const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
function base32Decode(s) {
  s = s.toUpperCase().replace(/=+$/g, '');
  let bits = 0, value = 0, out = [];
  for (const c of s) {
    const idx = B32.indexOf(c);
    if (idx < 0) continue;
    value = (value << 5) | idx; bits += 5;
    if (bits >= 8) { out.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
  }
  return Buffer.from(out);
}
function genSecret(len = 32) {
  const b = crypto.randomBytes(len);
  let s = '';
  for (const x of b) s += B32[x % 32];
  return s;
}
function totp(secret, timeStep = 30, digits = 6) {
  const key = base32Decode(secret);
  const counter = Math.floor(Date.now() / 1000 / timeStep);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const hmac = crypto.createHmac('sha1', key).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const bin = ((hmac[offset] & 0x7f) << 24) | (hmac[offset + 1] << 16) | (hmac[offset + 2] << 8) | hmac[offset + 3];
  return String(bin % Math.pow(10, digits)).padStart(digits, '0');
}
function verifyTotp(secret, code) {
  if (!/^\d{6}$/.test(code || '')) return false;
  for (const w of [0, -1, 1]) {
    if (totp(secret, 30, 6, w * 30) === code) return true;
  }
  return false;
}
let totpSecret = cfg.totpSecret || genSecret();
let totpBound = cfg.totpBound === true;   // 首次成功验证后置 true（二维码不再公开）
if (!cfg.totpSecret) { cfg.totpSecret = totpSecret; saveJson(CONFIG_FILE, cfg); }
function otpauthUri() {
  const issuer = encodeURIComponent('云桥Linux');
  return `otpauth://totp/${issuer}:admin?secret=${totpSecret}&issuer=${issuer}`;
}

// session（HttpOnly cookie）
const sessions = new Map(); // token -> expiry
function issueSession() {
  const t = crypto.randomBytes(24).toString('hex');
  sessions.set(t, Date.now() + 7 * 24 * 3600 * 1000); // 7 天
  return t;
}
function authed(req) {
  const cookie = (req.headers.cookie || '');
  const m = cookie.match(/(?:^|;\s*)__yk=([a-f0-9]+)/);
  if (!m) return false;
  const exp = sessions.get(m[1]);
  if (!exp || exp < Date.now()) { sessions.delete(m[1]); return false; }
  return true;
}

// ─── 登录页（现代深色）─────────────────────────
const LOGIN_HTML = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>云桥 Linux · 安全验证</title>
<script src="/qrcode.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter','Noto Sans SC',system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:radial-gradient(1200px 600px at 20% -10%,#1e3a5f 0%,transparent 55%),radial-gradient(900px 500px at 110% 110%,#3a1e5f 0%,transparent 50%),#0b0f1a;color:#e8ecf4;overflow:hidden}
.card{background:rgba(20,28,46,.72);backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,.08);border-radius:22px;
padding:40px 44px;width:390px;box-shadow:0 30px 80px rgba(0,0,0,.5)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.logo .dot{width:14px;height:14px;border-radius:50%;background:linear-gradient(135deg,#38bdf8,#6366f1);box-shadow:0 0 18px rgba(99,102,241,.8)}
.logo span{font-size:17px;font-weight:600;letter-spacing:.5px}
h2{font-size:20px;font-weight:600;margin:16px 0 4px}
.sub{color:#8b94a7;font-size:13px;margin-bottom:22px;line-height:1.6}
.steps{display:flex;flex-direction:column;gap:12px;margin-bottom:24px}
.step{display:flex;gap:12px;align-items:flex-start}
.step .n{flex:0 0 24px;height:24px;border-radius:50%;background:rgba(56,189,248,.15);color:#38bdf8;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.step .t{font-size:13px;color:#c2cadb;line-height:1.5}
#qrcode{display:flex;justify-content:center;background:#fff;padding:14px;border-radius:14px;margin:4px 0 18px}
#qrcode img{width:200px;height:200px;display:block}
.code-row{display:flex;gap:10px;justify-content:center;margin:6px 0 20px}
.code-row input{width:52px;height:60px;text-align:center;font-size:26px;font-weight:700;border-radius:12px;border:1px solid rgba(255,255,255,.14);
background:rgba(255,255,255,.06);color:#e8ecf4;outline:none;transition:.15s;caret-color:#38bdf8}
.code-row input:focus{border-color:#38bdf8;box-shadow:0 0 0 3px rgba(56,189,248,.18)}
.btn{width:100%;padding:14px;border:none;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;
background:linear-gradient(135deg,#38bdf8,#6366f1);color:#fff;transition:.15s}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn:disabled{opacity:.5;cursor:not-allowed}
#err{color:#f87171;font-size:13px;text-align:center;margin-top:14px;min-height:18px}
.foot{margin-top:18px;text-align:center;color:#566076;font-size:11px}
</style></head><body>
<div class="card">
  <div class="logo"><div class="dot"></div><span>云桥 Linux</span></div>
  <h2>安全验证</h2>
  <div class="sub">用手机上的验证器 App 扫码绑定，然后输入 6 位动态码进入。</div>
  <div class="steps">
    <div class="step"><div class="n">1</div><div class="t">打开 <b>Google 验证器</b> / <b>微软验证器</b> / <b>Authy</b></div></div>
    <div class="step"><div class="n">2</div><div class="t">扫描下方二维码（或手动输入密钥）</div></div>
    <div class="step"><div class="n">3</div><div class="t">输入 App 显示的 6 位动态码</div></div>
  </div>
  <div id="qrcode"></div>
  <div class="code-row" id="codeRow"></div>
  <button class="btn" id="btn">验证并进入</button>
  <div id="err"></div>
  <div class="foot">本机受 TOTP 双重保护 · 验证码 30 秒刷新</div>
</div>
<script>
(function () {
  var secretTxt = '';
  // 6 个输入框
  var row = document.getElementById('codeRow');
  var inputs = [];
  for (var i = 0; i < 6; i++) {
    var inp = document.createElement('input');
    inp.maxLength = 1; inp.inputMode = 'numeric';
    inp.addEventListener('input', function () {
      var v = this.value.replace(/\\D/g, '');
      this.value = v;
      if (v && this.nextElementSibling) this.nextElementSibling.focus();
    });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Backspace' && !this.value && this.previousElementSibling) this.previousElementSibling.focus();
      if (e.key === 'Enter') doLogin();
    });
    row.appendChild(inp); inputs.push(inp);
  }
  function showQr(uri, secret) {
    secretTxt = secret;
    var q = document.getElementById('qrcode');
    q.innerHTML = '';
    try {
      new QRCode(q, { text: uri, width: 200, height: 200, correctLevel: QRCode.CorrectLevel.M });
    } catch (e) {
      q.innerHTML = '<div style="color:#0a0a0a;font-size:13px;word-break:break-all;max-width:220px">手动输入密钥：<b style="font-family:monospace">' + secret + '</b></div>';
    }
    // 二维码下方附密钥（兜底手动输入）
  }
  function doLogin() {
    var code = inputs.map(function (i) { return i.value; }).join('');
    if (code.length < 6) { setErr('请输入 6 位验证码'); return; }
    document.getElementById('btn').disabled = true;
    fetch('/api/auth/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: code }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.success) { location.href = '/'; }
        else { setErr(d.error || '验证失败'); document.getElementById('btn').disabled = false; inputs.forEach(function (i) { i.value = ''; }); inputs[0].focus(); }
      })
      .catch(function () { setErr('网络错误'); document.getElementById('btn').disabled = false; });
  }
  function setErr(m) { document.getElementById('err').textContent = m; }
  document.getElementById('btn').addEventListener('click', doLogin);
  fetch('/api/auth/qr').then(function (r) { return r.json(); }).then(function (d) {
    if (!d.success) return;
    if (d.bound) {
      // 已绑定：隐藏二维码 + 扫码步骤，只留验证码输入
      document.getElementById('qrcode').style.display = 'none';
      var steps = document.querySelectorAll('.step');
      for (var i = 0; i < steps.length; i++) {
        var n = steps[i].querySelector('.n').textContent;
        if (n === '1' || n === '2') steps[i].style.display = 'none';
      }
    } else {
      showQr(d.uri, d.secret);
    }
  });
  setTimeout(function () { if (inputs[0]) inputs[0].focus(); }, 300);
})();
</script></body></html>`;

// ─── 前端桥接 shim ──────────────────────────────
const SHIM = `<script>
(function () {
  // 非 HTTPS 环境下 navigator.clipboard 不存在 → 提供 fallback（textarea + execCommand）
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      Object.defineProperty(navigator, 'clipboard', {
        value: {
          writeText: function (text) {
            return new Promise(function (resolve, reject) {
              try {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                var ok = document.execCommand('copy');
                document.body.removeChild(ta);
                if (ok) resolve(); else reject(new Error('copy failed'));
              } catch (e) { reject(e); }
            });
          },
          readText: function () { return Promise.resolve(''); }
        },
        configurable: true
      });
    }
  } catch (e) {}
  // ── 云桥 Linux WebUI 桥接层 ──
  var METHODS = ['get_status','save_settings','get_settings','toggle_connect','cancel_command','refresh_pair_code','send_message','get_codegraph_status','build_index','reorder_messages','delete_messages','edit_message','get_pending_messages','get_mcp_ticket','browse_folder','get_sessions','create_session','close_session','switch_session','set_permission','disconnect_agent','confirm_dialog','window_minimize','window_maximize','window_close','start_drag'];
  var api = {};
  METHODS.forEach(function (m) {
    api[m] = function () {
      var args = Array.prototype.slice.call(arguments);
      return fetch('/api/' + m, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ args: args }) })
        .then(function (r) { if (r.status === 401) { location.href = '/login'; return { success: false, error: '未认证' }; } return r.json(); });
    };
  });
  window.pywebview = { api: api };
  // ── 实时状态推送（模拟桌面版 notify_ui → handleBridge）──
  if (window.EventSource) {
    (function () {
      var es = new EventSource('/events');
      es.onmessage = function (ev) {
        try {
          var d = JSON.parse(ev.data);
          if (window.handleBridge) window.handleBridge(d.type || 'log', d);
        } catch (e) {}
      };
    })();
  }
})();
</script>`;

let uiCached = null;
function getUiHtml() {
  if (uiCached) return uiCached;
  try {
    let html = fs.readFileSync(UI_PATH, 'utf-8');
    if (html.indexOf('云桥 Linux WebUI 桥接层') === -1) {
      const marker = '<script>';
      const idx = html.indexOf(marker);
      if (idx >= 0) html = html.slice(0, idx) + SHIM + '\n' + html.slice(idx);
    }
    uiCached = html;
    return html;
  } catch (e) { return '<html><body><h3>ui.html 缺失: ' + e.message + '</h3></body></html>'; }
}

// ─── relay helper ───────────────────────────────
const httpMod = RELAY_URL.startsWith('https') ? require('https') : require('http');
function relayFetch(urlPath, opts) {
  const { method = 'GET', body } = opts || {};
  return new Promise((resolve) => {
    const u = new URL(RELAY_URL + urlPath);
    const data = body ? JSON.stringify(body) : null;
    const req = httpMod.request(u, { method, headers: { 'X-Key': RELAY_KEY, 'Content-Type': 'application/json' } }, (res) => {
      let buf = ''; res.on('data', c => buf += c); res.on('end', () => {
        try { resolve({ ok: res.statusCode < 400, status: res.statusCode, data: JSON.parse(buf) }); }
        catch (e) { resolve({ ok: res.statusCode < 400, status: res.statusCode, data: { raw: buf } }); }
      });
    });
    req.on('error', e => resolve({ ok: false, status: 0, error: e.message }));
    if (data) req.write(data);
    req.end();
  });
}

// ─── 本机状态 ───────────────────────────────────
function sysInfo() {
  try {
    return { hostname: os.hostname(), platform: os.platform(), arch: os.arch(), os: `${os.type()} ${os.release()}`,
      cpu: os.cpus().length + ' 核', memTotal: Math.round(os.totalmem() / 1048576) + ' MB',
      memFree: Math.round(os.freemem() / 1048576) + ' MB', uptime: Math.round(os.uptime()) + 's',
      loadavg: os.loadavg().map(v => v.toFixed(2)).join(' / ') };
  } catch (e) { return {}; }
}
function agentStatus() {
  return new Promise((resolve) => {
    exec('pgrep -f "tcp_agent.py.*--reverse" >/dev/null 2>&1 && echo running || echo stopped', (e, so) => {
      const running = so.trim() === 'running';
      let logTail = '';
      try { logTail = fs.readFileSync(AGENT_LOG, 'utf-8').split('\n').slice(-8).join('\n'); } catch (err) {}
      resolve({ running, connected: running && logTail.indexOf('relay_status') >= 0, logTail });
    });
  });
}
function readSessions() {
  const d = loadJson(SESSIONS_FILE, {});
  return { sessions: (d.sessions || []).map(s => ({ id: s.id, name: s.name, workDir: s.workDir, cwd: s.cwd || s.workDir })), defaultId: d.defaultId || null };
}
function writeSessions(list, defaultId) {
  return saveJson(SESSIONS_FILE, { sessions: list, defaultId });
}

// ─── tcp_agent 启停（连接/断开） ──────────────
function agentRunningP() {
  // [t]cp 方括号技巧：避免 pgrep 匹配到自身 exec shell
  return new Promise((resolve) => {
    exec('pgrep -f "[t]cp_agent.py.*--reverse" >/dev/null 2>&1 && echo yes || echo no', (e, so) => resolve(so.trim() === 'yes'));
  });
}
function stopAgentProc() {
  return new Promise((resolve) => {
    // pgrep 拿真实 PID 再 kill（避免 pkill -f 匹配 exec shell 自身/误杀）
    exec('for p in $(pgrep -f "[p]ython3 tcp_agent.py"); do kill $p 2>/dev/null; done; echo stopped', (e, so) => resolve());
  });
}
function startAgentProc() {
  // worker 连接的目标中转 = WebUI 配置里的 relayUrl（用户可在配置里改）
  let host = 'yunqiao.very.im';
  try { host = new URL(RELAY_URL).hostname; } catch (e) {}
  return new Promise((resolve) => {
    exec(`cd ${AGENT_DIR} && (setsid nohup python3 tcp_agent.py --reverse --relay-ip ${host} --relay-port 19998 > agent.log 2>&1 < /dev/null &)`, (e, so) => resolve());
  });
}

// ─── API ────────────────────────────────────────
async function handleApi(method, args) {
  args = args || [];
  switch (method) {
    case 'auth/qr': {
      // 已绑定 → 不再暴露二维码/secret（避免任何人扫码进入）
      if (totpBound) return { success: true, bound: true };
      return { success: true, uri: otpauthUri(), secret: totpSecret, bound: false };
    }
    case 'auth/verify': {
      const [code] = args;
      if (!verifyTotp(totpSecret, code)) return { success: false, error: '验证码错误或已过期' };
      if (!totpBound) { totpBound = true; cfg.totpBound = true; saveJson(CONFIG_FILE, cfg); }
      const token = issueSession();
      return { success: true, token };
    }
    case 'auth/logout': return { success: true };
    case 'get_status': {
      const st = await agentStatus();
      return { success: true, relayUrl: RELAY_URL, pairCode: (mcpTicket || pairCode).toString().slice(0, 6), relayConnected: st.connected, agentRunning: st.running, ...sysInfo() };
    }
    case 'get_settings': {
      // 脱敏：密钥只显示前4+****+后4，不泄露完整值
      let maskedKey = '';
      if (RELAY_KEY) maskedKey = RELAY_KEY.length > 8 ? RELAY_KEY.slice(0, 4) + '****' + RELAY_KEY.slice(-4) : '****';
      return { success: true, key: maskedKey, relayUrl: RELAY_URL, deviceName: os.hostname(), workDir: cfg.workDir || '', autoConnect: cfg.autoConnect || false, directMode: cfg.directMode || false };
    }
    case 'save_settings': {
      const [key, relay_url, auto_connect, direct_mode] = args;
      // 脱敏保护：key 含 **** 占位符 → 视为未修改，保持原值
      if (key && key.indexOf('****') === -1) RELAY_KEY = key;
      if (relay_url) RELAY_URL = relay_url.replace(/\/+$/, '');
      cfg.key = RELAY_KEY; cfg.relayUrl = RELAY_URL; cfg.autoConnect = auto_connect; cfg.directMode = direct_mode;
      cfg.pairCode = pairCode; cfg.deviceName = os.hostname(); saveJson(CONFIG_FILE, cfg);
      // 中转配置变化：网页开着则用新中转重启 worker
      if (sseCount > 0) {
        await stopAgentProc();
        await new Promise(r => setTimeout(r, 600));
        await startAgentProc();
      }
      return { success: true };
    }
    case 'get_sessions': return { success: true, ...readSessions() };
    case 'create_session': {
      const [work_dir, name] = args;
      if (!work_dir) return { success: false, error: '缺少工作目录' };
      try { fs.mkdirSync(work_dir, { recursive: true }); } catch (e) {}
      const d = readSessions(); const sid = 's-' + Date.now().toString(36);
      d.sessions.push({ id: sid, name: name || path.basename(work_dir), workDir: work_dir, cwd: work_dir });
      d.defaultId = sid; writeSessions(d.sessions, d.defaultId);
      return { success: true, sessionId: sid };
    }
    case 'switch_session': {
      const [sid] = args; const d = readSessions();
      if (!d.sessions.find(s => s.id === sid)) return { success: false, error: '会话不存在' };
      d.defaultId = sid; writeSessions(d.sessions, d.defaultId); return { success: true };
    }
    case 'close_session': {
      const [sid] = args; const d = readSessions();
      d.sessions = d.sessions.filter(s => s.id !== sid);
      if (d.defaultId === sid) d.defaultId = d.sessions[0]?.id || null;
      writeSessions(d.sessions, d.defaultId); return { success: true };
    }
    case 'send_message': {
      const [text, urgent] = args;
      if (!text) return { success: false, error: '内容为空' };
      const r = await relayFetch('/api/message', { method: 'POST', body: { text, urgent: !!urgent } });
      if (!r.ok) return { success: false, error: r.data?.error || r.error || 'relay 错误' };
      return { success: true, msgId: r.data.msgId || '' };
    }
    case 'get_pending_messages': {
      const r = await relayFetch('/api/message', { method: 'GET' });
      if (!r.ok) return { success: false, error: r.data?.error || 'relay 错误' };
      return { success: true, messages: r.data.messages || r.data.items || [] };
    }
    case 'delete_messages': { const [ids] = args; const r = await relayFetch('/api/message', { method: 'DELETE', body: { ids: ids || [] } }); return { success: r.ok, error: r.data?.error }; }
    case 'edit_message': { const [id, text] = args; const r = await relayFetch('/api/message', { method: 'PATCH', body: { id, text } }); return { success: r.ok, error: r.data?.error }; }
    case 'reorder_messages': { const [ids] = args; const r = await relayFetch('/api/message', { method: 'PUT', body: { orderedIds: ids || [] } }); return { success: r.ok, error: r.data?.error }; }
    case 'get_mcp_ticket': { const r = await relayFetch('/mcp-ticket', { method: 'GET' }); if (r.ok && r.data.ticket) mcpTicket = r.data.ticket; return { success: r.ok, ticket: r.data.ticket || '', error: r.data?.error }; }
    case 'refresh_pair_code': {
      pairCode = String(Math.floor(100000 + Math.random() * 900000));
      cfg.pairCode = pairCode; saveJson(CONFIG_FILE, cfg);
      const r = await relayFetch('/mcp-ticket', { method: 'GET' });
      if (r.ok && r.data.ticket) mcpTicket = r.data.ticket;
      return { success: true, pairCode: (mcpTicket || pairCode).toString().slice(0, 6) };
    }
    case 'cancel_command': { const r = await relayFetch('/api/cancel', { method: 'POST' }); return { success: r.ok, error: r.data?.error }; }
    case 'toggle_connect': {
      // 桌面版语义：连接=拉起 tcp_agent；断开=停掉它
      const running = await agentRunningP();
      if (running) await stopAgentProc(); else await startAgentProc();
      await new Promise(r => setTimeout(r, 800));
      const now = await agentRunningP();
      return { success: true, connected: now, running: now };
    }
    case 'disconnect_agent': {
      await stopAgentProc();
      return { success: true, connected: false, running: false };
    }
    case 'set_permission': return { success: true };
    case 'exec': {
      const [command] = args;
      if (!command) return { success: false, error: '缺少命令' };
      const low = command.trim().toLowerCase();
      if (low === 'rm -rf /' || low === 'mkfs' || low.indexOf('dd if=/dev') === 0) return { success: false, error: '危险命令被拒绝' };
      return new Promise((resolve) => {
        exec(command, { timeout: 60000, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) => {
          resolve({ success: true, stdout: stdout || '', stderr: stderr || '', exitCode: err ? err.code || 1 : 0 });
        });
      });
    }
    case 'browse_folder': {
      const [p] = args; const dir = p || os.homedir();
      try {
        const entries = fs.readdirSync(dir, { withFileTypes: true })
          .map(e => ({ name: e.name, isDir: e.isDirectory(), path: path.join(dir, e.name) }))
          .sort((a, b) => (b.isDir - a.isDir) || a.name.localeCompare(b.name));
        return { success: true, path: dir, entries: entries.slice(0, 500), parent: path.dirname(dir) };
      } catch (e) { return { success: false, error: e.message, path: dir }; }
    }
    case 'get_codegraph_status': {
      const [p] = args; const workDir = p || cfg.workDir || '';
      let installed = false;
      try { installed = execSync('which codegraph 2>/dev/null').toString().trim().length > 0; } catch (e) {}
      const indexed = workDir && fs.existsSync(path.join(workDir, '.codegraph'));
      return { success: true, workDir, codegraphInstalled: installed, indexed: !!indexed, hasCode: false, state: indexed ? 'indexed' : (installed ? 'ready' : 'not_installed') };
    }
    case 'build_index': {
      const [p] = args; const workDir = p || cfg.workDir || '';
      return new Promise((resolve) => { exec(`codegraph index "${workDir}"`, { timeout: 120000 }, (err, so, se) => resolve({ success: !err, error: err ? (se || '构建失败') : undefined })); });
    }
    case 'confirm_dialog': case 'window_minimize': case 'window_maximize': case 'window_close': case 'start_drag':
      return { success: true };
    default: return { success: false, error: '未知方法: ' + method };
  }
}

// ─── 实时状态推送（tcp_agent [UI] 行 → SSE → handleBridge）───
const sseClients = new Set();
let sseCount = 0;
let disconnectTimer = null;
// 网页会话 = 连接开关：有页面打开 → 确保 tcp_agent 连接；全关 → 延时断开
function onUiOpen() {
  sseCount++;
  clearTimeout(disconnectTimer);
  if (sseCount === 1) {
    agentRunningP().then(r => { if (!r) { console.log('[webui] 页面打开 → 启动 tcp_agent'); startAgentProc(); } });
  }
}
function onUiClose() {
  sseCount = Math.max(0, sseCount - 1);
  if (sseCount === 0) {
    clearTimeout(disconnectTimer);
    disconnectTimer = setTimeout(() => {
      if (sseCount === 0) { console.log('[webui] 页面全部关闭 → 断开 tcp_agent'); stopAgentProc(); }
    }, 8000);
  }
}
let uiLogOffset = 0;
try { uiLogOffset = fs.statSync(AGENT_LOG).size; } catch (e) {}
function broadcastUi(d) {
  const payload = `data: ${JSON.stringify(d)}\n\n`;
  for (const res of sseClients) { try { res.write(payload); } catch (e) { sseClients.delete(res); } }
}
setInterval(() => {
  try {
    const st = fs.statSync(AGENT_LOG);
    if (st.size < uiLogOffset) uiLogOffset = 0; // 文件轮转
    if (st.size > uiLogOffset) {
      const fd = fs.openSync(AGENT_LOG, 'r');
      const buf = Buffer.alloc(Math.min(st.size - uiLogOffset, 1 << 20));
      fs.readSync(fd, buf, 0, buf.length, uiLogOffset);
      fs.closeSync(fd);
      uiLogOffset = st.size;
      for (const line of buf.toString('utf-8').split('\n')) {
        const t = line.trim();
        if (t.startsWith('[UI] ')) {
          try { broadcastUi(JSON.parse(t.slice(5))); } catch (e) {}
        }
      }
    }
  } catch (e) {}
}, 1000);

// ─── HTTP 服务 ──────────────────────────────────
const server = http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, 'http://localhost');
    const p = u.pathname;
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

    // 登录页 + 认证静态资源（免认证）
    if (p === '/login') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(LOGIN_HTML); return; }
    if (p === '/qrcode.min.js') { try { res.writeHead(200, { 'Content-Type': 'text/javascript' }); res.end(fs.readFileSync(QR_PATH)); } catch (e) { res.writeHead(404); res.end(); } return; }
    if (p === '/tailwind.local.js') { try { res.writeHead(200, { 'Content-Type': 'text/javascript' }); res.end(fs.readFileSync(path.join(__dirname, 'tailwind.local.js'))); } catch (e) { res.writeHead(404); res.end(); } return; }

    // 认证 API（登录流程）
    if (p === '/api/auth/verify') {
      let body = ''; req.on('data', c => body += c); req.on('end', async () => {
        let code = ''; try { code = JSON.parse(body).code || ''; } catch (e) {}
        const r = await handleApi('auth/verify', [code]);
        if (r.success) {
          res.setHeader('Set-Cookie', `__yk=${r.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800`);
          res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ success: true }));
        } else { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(r)); }
      });
      return;
    }
    if (p === '/api/auth/qr') { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(await handleApi('auth/qr', []))); return; }
    if (p === '/api/auth/logout') {
      res.setHeader('Set-Cookie', '__yk=; Path=/; HttpOnly; Max-Age=0');
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ success: true })); return;
    }

    // 其余全部要求认证
    if (!authed(req)) {
      if (p === '/' || p === '/index.html') { res.writeHead(302, { Location: '/login' }); res.end(); return; }
      if (p.startsWith('/api/')) { res.writeHead(401, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ success: false, error: '未认证' })); return; }
      res.writeHead(302, { Location: '/login' }); res.end(); return;
    }

    // 实时事件流（SSE，需登录）
    if (p === '/events') {
      if (!authed(req)) { res.writeHead(401); res.end(); return; }
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive' });
      res.write(': connected\n\n');
      sseClients.add(res);
      onUiOpen();
      req.on('close', () => { sseClients.delete(res); onUiClose(); });
      return;
    }

    if (p === '/' || p === '/index.html') { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(getUiHtml()); return; }
    if (p === '/tailwind.local.js') { try { res.writeHead(200, { 'Content-Type': 'text/javascript' }); res.end(fs.readFileSync(path.join(__dirname, 'tailwind.local.js'))); } catch (e) { res.end(''); } return; }

    if (p.startsWith('/api/')) {
      const method = p.slice(5);
      let body = ''; req.on('data', c => body += c); req.on('end', async () => {
        let args = []; try { args = JSON.parse(body || '{}').args || []; } catch (e) {}
        try { const result = await handleApi(method, args); res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(result)); }
        catch (e) { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ success: false, error: e.message })); }
      });
      return;
    }
    res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('Not Found: ' + p);
  } catch (e) {
    res.writeHead(500, { 'Content-Type': 'text/plain' }); res.end('Server error: ' + e.message);
  }
});

server.listen(PORT, () => {
  console.log(`\n🌐 云桥 Linux WebUI: http://0.0.0.0:${PORT}（TOTP 认证）`);
  console.log(`   设备: ${os.hostname()} | relay: ${RELAY_URL}`);
});
