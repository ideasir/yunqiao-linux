"""
云桥 - TCP Agent 核心引擎
========================
通过反向 TCP 直连中继服务器，接收 JSON 命令并执行。
反向模式：客户端主动连接中继服务器的 19998 端口。

用法：
  python tcp_agent.py --reverse --relay-ip <IP> --relay-port 19998   # 反向连接中继
  python tcp_agent.py                                                # 正向监听模式（备用）
"""

import asyncio
import json
import os
import platform
import sys
import time
import uuid
import base64
import subprocess
import socket

# psutil 可选（用于进程管理），Windows 上 fallback 到 tasklist/taskkill
try:
    import psutil as _psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    _psutil = None

# 尝试导入 websockets（用于代理穿透，可选）
try:
    import websockets
    HAS_WS = True
except ImportError:
    HAS_WS = False
from pathlib import Path
from common import Session, SessionManager



# ═══════════════════════════════════════════════════════════
# 会话管理（复用 agent.py 逻辑）


# ═══════════════════════════════════════════════════════════
# TCP Agent
# ═══════════════════════════════════════════════════════════

class TCPAgent:
    """TCP Agent 核心引擎
    
    通过反向 TCP 直连中继服务器，接收 JSON 命令并执行。
    """
    
    def __init__(self, host="0.0.0.0", port=19999):
        self.host = host
        self.port = port
        self.device_name = platform.node()
        self.auth_code = None
        # 活动中的命令进程（支持 cancel 中断）
        self._active_procs = set()
        
        # 权限模式
        self.permission = "workspace"
        
        # 会话管理
        self.sessions = SessionManager()
        self.sessions.load()
        
        # 默认工作区
        if getattr(sys, 'frozen', False):
            base_dir = Path(sys.executable).parent
        else:
            base_dir = Path(__file__).parent.parent
        self.default_work_dir = str(base_dir / 'worker')
        os.makedirs(self.default_work_dir, exist_ok=True)
        if not self.sessions.sessions:
            self.sessions.create(self.default_work_dir, '默认工作区')
        
        # 回调
        self.on_log = lambda msg: print(f"[tcp-agent] {msg}")
        self.on_connected = lambda: None
        
        self._server = None
        self._running = False

    def start(self):
        """启动 TCP 服务端"""
        if self._running:
            return
        self._running = True
        self.on_log(f"监听 {self.host}:{self.port}")
        asyncio.run(self._serve())
    
    async def _serve(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self.on_connected()
        self.on_log(f"TCP 服务器已启动: {self.host}:{self.port}")
        async with self._server:
            await self._server.serve_forever()
    
    async def _handle_client(self, reader, writer):
        """处理客户端连接"""
        peer = writer.get_extra_info('peername')
        self.on_log(f"新连接: {peer}")
        try:
            # 接收消息（4 字节长度前缀 + JSON）
            data = await self._read_message(reader)
            if not data:
                return
            msg = json.loads(data)
            self.on_log(f"收到命令: {msg.get('type', '?')}")
            
            # 处理命令
            result = await self._handle_command(msg)
            
            # 返回结果
            await self._send_message(writer, result)
            
        except Exception as e:
            self.on_log(f"处理错误: {e}")
            try:
                await self._send_message(writer, {"error": str(e)})
            except:
                pass
        finally:
            writer.close()
    
    async def _read_message(self, reader):
        """读取消息：4字节长度前缀 + JSON"""
        header = await reader.readexactly(4)
        length = int.from_bytes(header, 'big')
        if length > 10 * 1024 * 1024:  # 10MB 限制
            raise ValueError("消息过长")
        return await reader.readexactly(length)
    
    async def _send_message(self, writer, obj):
        """发送消息：4字节长度前缀 + JSON"""
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        writer.write(len(data).to_bytes(4, 'big'))
        writer.write(data)
        await writer.drain()
    
    async def _handle_command(self, msg):
        """处理命令"""
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        
        # 服务器推送（agent_status/agent_keepalive 等）→ 转发给桌面 UI
        if msg_type == "push":
            inner = payload or {}
            try:
                # ensure_ascii=True（默认）：管道内全 ASCII，避免 Windows GBK 管道编码导致中文乱码
                print(f"[UI] {json.dumps(inner)}", flush=True)
            except Exception:
                pass
            return {"type": "push_ack", "ok": True}
        
        if msg_type == "register":
            return {"success": True, "type": "register_result", "deviceId": self.device_name}
        
        elif msg_type == "execute_command":
            command = payload.get("command", "")
            timeout = payload.get("timeout", 30000)
            shell = payload.get("shell", False)
            session = self.sessions.get_current()
            cwd = session.cwd if session else os.getcwd()
            if shell:
                # 走临时脚本机制免转义
                result = await self._exec_script("cmd", command, cwd, timeout)
            else:
                result = await self._exec_cmd(command, timeout, cwd)
            return {"type": "command_result", "result": result}
        
        elif msg_type == "exec_script":
            language = payload.get("language", "auto")
            code = payload.get("code", "")
            script_b64 = payload.get("script_b64")
            cwd = payload.get("cwd")
            timeout = payload.get("timeout", 120000)
            result = await self._exec_script(language, code, cwd, timeout, script_b64)
            return {"type": "script_result", "result": result}
        
        elif msg_type == "run_custom":
            name = payload.get("name", "")
            args = payload.get("args", [])
            timeout = payload.get("timeout", 120000)
            result = await self._run_custom(name, args, timeout)
            return {"type": "custom_result", "result": result}
        
        elif msg_type == "get_environment":
            result = await self._get_environment()
            return {"type": "environment_result", "result": result}
        
        elif msg_type == "notify":
            text = payload.get("text", "")
            # 通知 → 桌面 UI 日志
            try:
                print(f"[UI] {json.dumps({'type': 'log', 'text': '🔔 ' + text})}", flush=True)
            except Exception:
                pass
            return {"type": "notify_ack", "success": True, "text": text}
        
        elif msg_type == "read_file":
            path = payload.get("path", "")
            result = self._read_file(path)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "write_file":
            path = payload.get("path", "")
            result = self._write_file(path, payload.get("content", ""))
            return {"type": "file_result", "result": result}
        
        elif msg_type == "get_device_info":
            return {"type": "device_info", "result": self._get_info()}
        
        elif msg_type == "download":
            path = payload.get("path", "")
            result = self._download(path)
            return {"type": "download_result", "result": result}
        
        elif msg_type == "session_op":
            op = payload.get("op", "")
            result = await self._handle_session_op(op, payload)
            return {"type": "session_op_result", "result": result}
        
        elif msg_type == "write_file_b64":
            path = payload.get("path", "")
            content_b64 = payload.get("content_base64", "")
            result = self._write_file_b64(path, content_b64)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "edit_file":
            path = payload.get("path", "")
            old_text = payload.get("old_text", "")
            new_text = payload.get("new_text", "")
            result = self._edit_file(path, old_text, new_text)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "move_file":
            src = payload.get("src", "")
            dst = payload.get("dst", "")
            result = self._move_file(src, dst)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "delete_file":
            path = payload.get("path", "")
            result = self._delete_file(path)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "mk_dirs":
            path = payload.get("path", "")
            result = self._mk_dirs(path)
            return {"type": "file_result", "result": result}
        
        elif msg_type == "resolve_path":
            path = payload.get("path", "")
            result = self._resolve_path(path)
            return {"type": "path_result", "result": result}
        
        elif msg_type == "list_processes":
            pattern = payload.get("pattern")
            result = await self._list_processes(pattern)
            return {"type": "process_result", "result": result}
        
        elif msg_type == "kill_process":
            target = payload.get("target", "")
            result = await self._kill_process(target)
            return {"type": "process_result", "result": result}
        
        elif msg_type == "start_process":
            command = payload.get("command", "")
            cwd = payload.get("cwd")
            redirect_log = payload.get("redirectLog")
            result = await self._start_process(command, cwd, redirect_log)
            return {"type": "process_result", "result": result}
        
        elif msg_type == "tail_log":
            path = payload.get("path", "")
            offset = payload.get("offset", 0)
            max_bytes = payload.get("maxBytes")
            result = self._tail_log(path, offset, max_bytes)
            return {"type": "log_result", "result": result}
        
        elif msg_type == "ping":
            # 回显服务器时间戳，服务器据此算 RTT
            return {"type": "pong", "ts": payload.get("ts", 0)}
        
        else:
            return {"type": "error", "error": f"未知命令: {msg_type}"}
    
    # ── 命令执行 ──
    
    async def _exec_cmd(self, command, timeout, cwd=None):
        def _decode(b):
            if not b: return ""
            for enc in ['utf-8', 'gbk']:
                try: return b.decode(enc)
                except: continue
            return b.decode('utf-8', errors='replace')
        
        t0 = time.time()
        # 检查 cwd 有效性，无效则回退到当前目录
        if cwd and not os.path.isdir(cwd):
            cwd = os.getcwd()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            # 注册为活动进程（支持 cancel 中断）
            self._active_procs.add(proc)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout / 1000
                )
                return {"exitCode": proc.returncode or 0,
                        "stdout": _decode(stdout),
                        "stderr": _decode(stderr),
                        "killed": False,
                        "duration": int((time.time() - t0) * 1000),
                        "resolvedCwd": cwd or os.getcwd()}
            except asyncio.TimeoutError:
                proc.kill()
                # kill 后 communicate 可能挂（Windows 子进程树），加短超时保护
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                except Exception:
                    stdout, stderr = b"", b""
                return {"exitCode": 1,
                        "stdout": _decode(stdout) if stdout else "",
                        "stderr": _decode(stderr) if stderr else "",
                        "killed": True,
                        "duration": int((time.time() - t0) * 1000),
                        "resolvedCwd": cwd or os.getcwd()}
            finally:
                self._active_procs.discard(proc)
        except Exception as e:
            return {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False, "duration": 0, "resolvedCwd": cwd or os.getcwd()}

    # ── 亲和通道：脚本执行（写临时文件执行，避开转义）──
    async def _exec_script(self, language, code, cwd=None, timeout=120000, script_b64=None):
        import tempfile
        t0 = time.time()
        # P2-2: 支持 base64 传入脚本，避免 JSON 转义问题
        if script_b64:
            try:
                code = base64.b64decode(script_b64).decode("utf-8")
            except Exception as e:
                return {"exitCode": 1, "stdout": "", "stderr": f"script_b64 解码失败: {e}", "killed": False, "duration": 0, "language": "error"}
        # 语言 → 文件后缀 + 解释器命令
        lang = (language or "auto").lower()
        ext_map = {"python": ".py", "py": ".py", "powershell": ".ps1", "ps1": ".ps1",
                   "node": ".js", "js": ".js", "bash": ".sh", "sh": ".sh", "cmd": ".bat", "bat": ".bat"}
        if lang == "auto" or lang not in ext_map:
            # auto 检测：优先 python
            lang = "python"
        ext = ext_map.get(lang, ".py")
        tmp = os.path.join(tempfile.gettempdir(), f"yq_script_{uuid.uuid4().hex[:8]}{ext}")
        try:
            # PowerShell 5.1 需要 BOM 才能正确解析 UTF-8 中文
            enc = "utf-8-sig" if lang in ("powershell", "ps1") else "utf-8"
            with open(tmp, "w", encoding=enc) as f:
                f.write(code or "")
            # Windows 上先设 chcp 65001 确保中文输出不乱码
            prefix = "chcp 65001 >nul && " if sys.platform == "win32" else ""
            if lang == "python":
                py = "python3" if sys.platform != "win32" else "python"
                cmd = f'{prefix}{py} "{tmp}"'
            elif lang == "powershell":
                cmd = f'{prefix}powershell -ExecutionPolicy Bypass -File "{tmp}"'
            elif lang == "node":
                cmd = f'{prefix}node "{tmp}"'
            elif lang == "bash":
                cmd = f'{prefix}bash "{tmp}"'
            else:  # cmd/bat
                cmd = f'{prefix}"{tmp}"'
            result = await self._exec_cmd(cmd, timeout, cwd)
            result["language"] = lang
            return result
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

    # ── 自定义命令：custom-commands/ 目录白名单脚本 ──
    async def _run_custom(self, name, args=None, timeout=120000):
        t0 = time.time()
        # 命令名安全校验：禁止路径穿越
        if not name or "/" in name or "\\" in name or ".." in name:
            return {"exitCode": 1, "stdout": "", "stderr": "非法命令名", "killed": False, "duration": 0}
        # 脚本目录：client/custom-commands/
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).parent
        cmd_dir = base / "custom-commands"
        candidates = [cmd_dir / f"{name}.py", cmd_dir / f"{name}.ps1", cmd_dir / f"{name}.sh",
                      cmd_dir / f"{name}.bat", cmd_dir / f"{name}.js"]
        script = next((p for p in candidates if p.exists()), None)
        if not script:
            return {"exitCode": 1, "stdout": "", "stderr": f"自定义命令脚本不存在: {name}", "killed": False, "duration": 0}
        args_str = " ".join(f'"{a}"' for a in (args or []))
        if script.suffix == ".py":
            cmd = f'python "{script}" {args_str}'
        elif script.suffix == ".ps1":
            cmd = f'powershell -ExecutionPolicy Bypass -File "{script}" {args_str}'
        elif script.suffix == ".js":
            cmd = f'node "{script}" {args_str}'
        elif script.suffix == ".sh":
            cmd = f'bash "{script}" {args_str}'
        else:
            cmd = f'"{script}" {args_str}'
        result = await self._exec_cmd(cmd, timeout, None)
        return result

    # ── 环境自述：环境档案 ──
    async def _get_environment(self):
        def _which(cmd):
            try:
                import shutil
                return shutil.which(cmd) is not None
            except Exception:
                return False

        interps = {}
        for name, cmds in [("python", ["python", "python3"]), ("node", ["node"]),
                           ("powershell", ["powershell", "pwsh"]), ("bash", ["bash"]),
                           ("cmd", ["cmd"])]:
            interps[name] = any(_which(c) for c in cmds)
        tools = [t for t in ["git", "curl", "grep", "find", "jq", "tar", "pip", "npm", "docker"] if _which(t)]
        session = self.sessions.get_current()
        return {
            "os": platform.system(),
            "hostname": self.device_name,
            "interpreters": interps,
            "tools": tools,
            "workDir": session.cwd if session else self.default_work_dir,
            "sessions": len(self.sessions.sessions),
        }
    
    def _read_file(self, path):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return {"success": True, "content": content, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _write_file(self, path, content):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _get_info(self):
        return {
            "hostname": platform.node(),
            "platform": sys.platform,
            "arch": platform.machine(),
            "cpuCores": os.cpu_count() or 0,
            "uptime": int(time.time() - (_psutil.boot_time() if HAS_PSUTIL else 0)),
            "homeDir": str(Path.home()),
            "user": os.environ.get("USERNAME", ""),
        }
    
    def _download(self, path):
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            size = os.path.getsize(path)
            return {"success": True, "data": data, "size": size, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ── P0-1: 文件写/改工具 ──
    
    def _write_file_b64(self, path, content_base64):
        """解码 base64 后写文件"""
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = base64.b64decode(content_base64)
            with open(path, "wb") as f:
                f.write(data)
            size = os.path.getsize(path)
            return {"success": True, "path": path, "size": size}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _edit_file(self, path, old_text, new_text):
        """精确文本替换（替换首次出现的 old_text）"""
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if old_text not in content:
                return {"success": False, "error": "old_text not found in file"}
            new_content = content.replace(old_text, new_text, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {"success": True, "path": path, "replaced": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _move_file(self, src, dst):
        """移动/重命名文件"""
        try:
            if not os.path.isabs(src):
                session = self.sessions.get_current()
                src = os.path.join(session.cwd, src) if session else src
            if not os.path.isabs(dst):
                session = self.sessions.get_current()
                dst = os.path.join(session.cwd, dst) if session else dst
            src = os.path.normpath(src)
            dst = os.path.normpath(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            return {"success": True, "src": src, "dst": dst}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _delete_file(self, path):
        """删除文件或目录（非空目录递归删除）"""
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
            else:
                os.remove(path)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _mk_dirs(self, path):
        """递归创建目录"""
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            os.makedirs(path, exist_ok=True)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ── P0-2: 路径解析工具 ──
    
    def _resolve_path(self, path):
        """解析路径类型和真实路径"""
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            real_path = os.path.realpath(path)
            exists = os.path.exists(path)
            is_link = os.path.islink(path)
            link_target = os.readlink(path) if is_link else None
            # Python 3.12+ 支持 os.path.isjunction
            is_junction = False
            if hasattr(os.path, 'isjunction'):
                is_junction = os.path.isjunction(path)
            return {
                "success": True,
                "path": path,
                "realPath": real_path,
                "exists": exists,
                "isLink": is_link,
                "linkTarget": link_target,
                "isJunction": is_junction,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ── P1-1: 进程管理三件套 ──
    
    async def _list_processes(self, pattern=None):
        """列出进程列表，支持按名称/命令行过滤。优先用 psutil，回退到 tasklist"""
        try:
            procs = []
            if HAS_PSUTIL:
                for p in _psutil.process_iter(['pid', 'name', 'cmdline']):
                    try:
                        info = p.info
                        cmdline = ' '.join(info.get('cmdline') or [])
                        if pattern:
                            name = (info.get('name') or '').lower()
                            cmdline_lower = cmdline.lower()
                            pat = pattern.lower()
                            if pat not in name and pat not in cmdline_lower:
                                continue
                        procs.append({
                            "pid": info['pid'],
                            "name": info.get('name', ''),
                            "cmdline": cmdline,
                        })
                    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                        continue
            else:
                # 回退到 tasklist（Windows 自带）
                import subprocess as _sp
                result = _sp.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, timeout=10)
                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        pid = parts[1].strip('"')
                        name = parts[0].strip('"')
                        if pattern:
                            pat = pattern.lower()
                            if pat not in name.lower() and pat not in pid:
                                continue
                        procs.append({"pid": int(pid), "name": name, "cmdline": ""})
            return {"success": True, "processes": procs, "count": len(procs)}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _kill_process(self, target):
        """按 PID 或命令行匹配杀进程"""
        try:
            killed = []
            if HAS_PSUTIL:
                try:
                    pid = int(target)
                    p = _psutil.Process(pid)
                    name = p.name()
                    p.kill()
                    killed.append({"pid": pid, "name": name})
                    return {"success": True, "killed": killed, "count": len(killed)}
                except ValueError:
                    pass  # 不是数字，按命令行匹配
                
                pat = target.lower()
                for p in _psutil.process_iter(['pid', 'name', 'cmdline']):
                    try:
                        cmdline = ' '.join(p.info.get('cmdline') or []).lower()
                        name = (p.info.get('name') or '').lower()
                        if pat in cmdline or pat in name:
                            pid = p.info['pid']
                            pname = p.info.get('name', '')
                            p.kill()
                            killed.append({"pid": pid, "name": pname})
                    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                        continue
            else:
                # 回退到 taskkill（Windows 自带）
                import subprocess as _sp
                try:
                    pid = int(target)
                    _sp.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, timeout=10)
                    killed.append({"pid": pid, "name": str(pid)})
                    return {"success": True, "killed": killed, "count": len(killed)}
                except ValueError:
                    pass
                _sp.run(['taskkill', '/F', '/IM', target], capture_output=True, timeout=10)
                killed.append({"name": target})
            return {"success": True, "killed": killed, "count": len(killed)}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _start_process(self, command, cwd=None, redirect_log=None):
        """后台启动进程，可选日志重定向"""
        try:
            if redirect_log:
                log_dir = os.path.dirname(redirect_log)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
                log_file = open(redirect_log, "a", encoding="utf-8")
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=cwd,
                    stdout=log_file,
                    stderr=log_file,
                )
                log_file.close()
            else:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=cwd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return {
                "success": True,
                "pid": proc.pid,
                "command": command,
                "logPath": redirect_log,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ── P2-1: 日志流读取 ──
    
    def _tail_log(self, path, offset=0, max_bytes=None):
        """从文件末尾读取增量内容"""
        try:
            if not os.path.isabs(path):
                session = self.sessions.get_current()
                path = os.path.join(session.cwd, path) if session else path
            path = os.path.normpath(path)
            if not os.path.exists(path):
                return {"success": True, "content": "", "size": 0, "offset": offset}
            file_size = os.path.getsize(path)
            if offset > file_size:
                offset = file_size
            read_size = file_size - offset
            if max_bytes and read_size > max_bytes:
                offset = file_size - max_bytes
                read_size = max_bytes
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read(read_size)
            content = data.decode("utf-8", errors="replace")
            return {
                "success": True,
                "content": content,
                "size": file_size,
                "offset": file_size,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _handle_session_op(self, op, payload):
        if op == "exec":
            command = payload.get("command", "")
            timeout = payload.get("timeout", 30000)
            shell = payload.get("shell")
            session = self.sessions.get_current()
            cwd = payload.get("cwd") or (session.cwd if session else os.getcwd())
            if shell:
                return await self._exec_script("cmd", command, cwd, timeout)
            return await self._exec_cmd(command, timeout, cwd)
        elif op == "read_file":
            return self._read_file(payload.get("path", ""))
        elif op == "write_file":
            return self._write_file(payload.get("path", ""), payload.get("content", ""))
        elif op == "create":
            return self.sessions.create(payload.get("workDir", ""), payload.get("name"))
        elif op == "close":
            return self.sessions.close(payload.get("sessionId"))
        elif op == "list":
            return self.sessions.list_all()
        elif op == "switch":
            return self.sessions.switch(payload.get("sessionId"))
        return {"error": f"未知会话操作: {op}"}


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="云桥 TCP Agent")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=19999, help="监听端口")
    parser.add_argument("--reverse", action="store_true", help="反向连接模式：主动连接中继服务器的 TCP 端口")
    parser.add_argument("--relay-ip", default="your-server.com", help="中继服务器 IP（公网）")
    parser.add_argument("--relay-port", type=int, default=19998, help="中继服务器反向 TCP 端口")
    args = parser.parse_args()
    
    if args.reverse:
        # 反向连接模式：Windows 主动连接中继服务器
        # 使用 4 字节长度前缀协议（与服务器 sendViaTCP 兼容）
        import asyncio
        # stdout 行缓冲：确保 [UI] 推送实时到达桌面端（重定向到管道时默认块缓冲会延迟）
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
        
        MAX_RETRIES = 9999  # 无限重试
        RETRY_DELAY = 5     # 重试间隔（秒）
        
        async def connect_reverse():
            retries = 0
            ws_retries = 0
            WS_PORT = 9876  # HTTP 端口（WebSocket 走同一端口，可过代理）
            while retries < MAX_RETRIES:
                try:
                    # 尝试 TCP 连接（直连，低延迟）
                    use_ws = False
                    if retries >= 1 and HAS_WS:
                        use_ws = True  # TCP 一次失败就换 WebSocket（代理环境）
                    
                    if use_ws:
                        ws_url = f"ws://{args.relay_ip}:{WS_PORT}/ws"
                        print(f'[tcp-agent] 尝试 WebSocket 连接: {ws_url}')
                        ws = await websockets.connect(ws_url, ping_interval=20, ping_timeout=10)
                        proto = 'ws'
                        # 包装 WebSocket 为流式接口
                        async def _read_ws(n):
                            data = await ws.recv()
                            if isinstance(data, bytes):
                                return data
                            return data.encode('utf-8')
                        async def _write_ws(data):
                            await ws.send(data)
                        _ws_close = ws.close
                        print(f'[tcp-agent] 已通过 WebSocket 连接到中继服务器')
                    else:
                        reader, writer = await asyncio.open_connection(args.relay_ip, args.relay_port)
                        print(f'[tcp-agent] 已连接到中继服务器: {args.relay_ip}:{args.relay_port}')
                        proto = 'tcp'
                        # 给 socket 设置 keepalive
                        sock = writer.get_extra_info('socket')
                        if sock:
                            try:
                                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                            except Exception:
                                pass
                    
                    # 创建 TCP Agent 实例处理命令
                    agent = TCPAgent(host=args.host, port=args.port)
                    
                    # 延迟探测：每 5 秒发 ping（TCP 和 WS 都走相同协议）
                    async def latency_probe():
                        while True:
                            try:
                                t0 = time.time()
                                ping = json.dumps({"type": "ping", "ts": int(t0 * 1000)}).encode('utf-8')
                                if use_ws:
                                    await ws.send(len(ping).to_bytes(4, 'big') + ping)
                                else:
                                    writer.write(len(ping).to_bytes(4, 'big'))
                                    writer.write(ping)
                                    await writer.drain()
                            except Exception:
                                return
                            await asyncio.sleep(5)
                    
                    probe_task = asyncio.ensure_future(latency_probe())
                    
                    # 用反向连接作为命令通道（4 字节长度前缀协议）
                    while True:
                        try:
                            # 读取 4 字节长度前缀
                            if use_ws:
                                raw = await ws.recv()
                                if isinstance(raw, str):
                                    raw = raw.encode('utf-8')
                                # WebSocket 消息可能是完整帧（含长度前缀）
                                buf = raw
                                if len(buf) < 4:
                                    continue
                                header = buf[:4]
                                length = int.from_bytes(header, 'big')
                                if length > 10 * 1024 * 1024:
                                    raise ValueError('消息过长')
                                # 检查是否完整消息
                                if len(buf) < 4 + length:
                                    continue
                                body = buf[4:4+length]
                            else:
                                header = await reader.readexactly(4)
                                length = int.from_bytes(header, 'big')
                                if length > 10 * 1024 * 1024:
                                    raise ValueError('消息过长')
                                body = await reader.readexactly(length)
                            msg = json.loads(body.decode('utf-8'))
                            # pong
                            if msg.get("type") == "pong":
                                t1 = time.time()
                                rtt = int((t1 * 1000) - (msg.get("ts") or t1 * 1000))
                                try:
                                    print(f"[UI] {json.dumps({'type': 'relay_status', 'status': 'connected', 'latency': max(rtt, 1), 'proto': proto})}", flush=True)
                                except Exception:
                                    pass
                                continue
                            # cancel
                            if msg.get("type") == "cancel":
                                n = 0
                                for p in list(agent._active_procs):
                                    try:
                                        p.kill()
                                        n += 1
                                    except Exception:
                                        pass
                                ack2 = json.dumps({"type": "cancel_ack", "killed": n}, ensure_ascii=False).encode('utf-8')
                                pkt = len(ack2).to_bytes(4, 'big') + ack2
                                if use_ws:
                                    await ws.send(pkt)
                                else:
                                    writer.write(pkt)
                                    await writer.drain()
                                print(f"[tcp-agent] 已取消 {n} 个活动进程", flush=True)
                                continue
                            # exec_task
                            if msg.get("type") == "exec_task":
                                payload = msg.get("payload", {})
                                task_id = payload.get("taskId", "")
                                command = payload.get("command", "")
                                timeout = payload.get("timeout", 1800000)
                                session = agent.sessions.get_current()
                                cwd = session.cwd if session else os.getcwd()
                                ack = json.dumps({"type": "task_ack", "taskId": task_id, "ok": True}, ensure_ascii=False).encode('utf-8')
                                pkt = len(ack).to_bytes(4, 'big') + ack
                                if use_ws:
                                    await ws.send(pkt)
                                else:
                                    writer.write(pkt)
                                    await writer.drain()
                                async def _run_task():
                                    try:
                                        result = await agent._exec_cmd(command, timeout, cwd)
                                    except Exception as e:
                                        result = {"exitCode": 1, "stdout": "", "stderr": str(e), "killed": False, "duration": 0}
                                    try:
                                        done = json.dumps({"type": "task_done", "taskId": task_id, "result": result}, ensure_ascii=False).encode('utf-8')
                                        pkt = len(done).to_bytes(4, 'big') + done
                                        if use_ws:
                                            await ws.send(pkt)
                                        else:
                                            writer.write(pkt)
                                            await writer.drain()
                                    except Exception:
                                        pass
                                asyncio.ensure_future(_run_task())
                                continue
                            # 处理命令
                            try:
                                result = await agent._handle_command(msg)
                            except Exception as e:
                                print(f'[tcp-agent] 处理命令异常: {e}', flush=True)
                                result = {"type": "error", "error": f"命令处理异常: {e}"}
                            resp_data = json.dumps(result, ensure_ascii=False).encode('utf-8')
                            pkt = len(resp_data).to_bytes(4, 'big') + resp_data
                            if use_ws:
                                await ws.send(pkt)
                            else:
                                writer.write(pkt)
                                await writer.drain()
                        except asyncio.IncompleteReadError:
                            print('[tcp-agent] 反向连接断开（服务器关闭）')
                            break
                        except Exception as e:
                            print(f'[tcp-agent] 反向连接错误: {e}')
                            break
                    probe_task.cancel()
                    if use_ws:
                        await ws.close()
                    else:
                        writer.close()
                    retries = 0  # 连接成功退出
                except (OSError, ConnectionError) as e:
                    print(f'[tcp-agent] 连接失败: {e}')
                    if retries >= 3 and not HAS_WS:
                        print('[tcp-agent] TCP 多次失败，未安装 websockets 库，无法切换')
                except Exception as e:
                    print(f'[tcp-agent] 意外错误: {e}')
                
                retries += 1
                print(f'[tcp-agent] {RETRY_DELAY} 秒后重试 ({retries})...')
                await asyncio.sleep(RETRY_DELAY)
        
        asyncio.run(connect_reverse())
    else:
        agent = TCPAgent(host=args.host, port=args.port)
        agent.on_log = lambda msg: print(f"[tcp-agent] {msg}")
        agent.start()