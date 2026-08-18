"""云桥 - 公共模块"""

import json
import os
import time
import uuid
from pathlib import Path


class Session:
    def __init__(self, sid, name, work_dir):
        self.id = sid
        self.name = name
        self.workDir = work_dir
        self.cwd = work_dir
        self.alive = True
        self.lastActive = time.time()

    def to_dict(self):
        return {"id": self.id, "name": self.name, "workDir": self.workDir,
                "cwd": self.cwd, "alive": self.alive, "lastActive": self.lastActive}


class SessionManager:
    def __init__(self, reload_on_access=False):
        self.sessions = []
        self.default_id = None
        self._reload_on_access = reload_on_access

    def _save_path(self):
        return os.path.join(os.environ.get("YUNQIAO_CONFIG", str(Path.home() / ".yunqiao")), "sessions.json")

    def _load(self):
        self.sessions = []
        self.default_id = None
        p = self._save_path()
        if os.path.exists(p):
            try:
                data = json.loads(open(p, "r", encoding="utf-8").read())
                for s_data in data.get("sessions", []):
                    s = Session(s_data["id"], s_data.get("name", ""), s_data.get("workDir", ""))
                    s.cwd = s_data.get("cwd", s.workDir)
                    s.lastActive = s_data.get("lastActive", time.time())
                    s.alive = s_data.get("alive", True)
                    self.sessions.append(s)
                self.default_id = data.get("defaultId")
            except Exception:
                pass

    def load(self):
        self._load()

    def _save(self):
        try:
            p = self._save_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w", encoding="utf-8").write(json.dumps({
                "sessions": [s.to_dict() for s in self.sessions],
                "defaultId": self.default_id,
            }, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def _ensure_loaded(self):
        if self._reload_on_access:
            self._load()

    def create(self, work_dir, name=None):
        self._ensure_loaded()
        sid = uuid.uuid4().hex[:8]
        name = name or f"session-{sid}"
        s = Session(sid, name, work_dir)
        self.sessions.append(s)
        self.default_id = sid
        self._save()
        return {"success": True, "id": sid, "name": name, "workDir": work_dir, "cwd": work_dir}

    def get_current(self):
        self._ensure_loaded()
        for s in self.sessions:
            if s.id == self.default_id:
                s.lastActive = time.time()
                return s
        return None

    def close(self, session_id=None):
        self._ensure_loaded()
        sid = session_id or self.default_id
        for s in self.sessions:
            if s.id == sid:
                self.sessions.remove(s)
                if self.default_id == sid:
                    self.default_id = self.sessions[0].id if self.sessions else None
                self._save()
                return {"success": True}
        return {"success": False, "error": "session not found: " + str(sid)}

    def list_all(self):
        self._ensure_loaded()
        return {"sessions": [s.to_dict() for s in self.sessions], "defaultId": self.default_id}

    def switch(self, session_id):
        self._ensure_loaded()
        for s in self.sessions:
            if s.id == session_id:
                self.default_id = session_id
                self._save()
                return {"success": True, "sessionId": s.id, "name": s.name, "workDir": s.workDir}
        return {"success": False, "error": "session not found: " + str(session_id)}
