#!/usr/bin/env python3
"""
容器内进程编排 —— PID 1。

职责:
  1. 从环境变量渲染 cliproxy 的 config.yaml（敏感串绝不进镜像）
  2. 按 processes.jsonc 拉起所有子进程，挂了自动重启
  3. 收到 SIGTERM 优雅传递给所有子进程（HF 重启 Space 时要干净退出）

为什么不用 supervisord: 多一层依赖、日志还得单独配。这里逻辑就百来行，自己写更可控。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parent
PROCS_FILE = Path(os.getenv("PROCESSES_FILE", str(APP / "processes.jsonc")))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))

_stop = threading.Event()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [sup] {msg}", flush=True)


def strip_jsonc(text: str) -> str:
    out, i, n = [], 0, len(text)
    in_str, quote = False, ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == quote:
                in_str = False
            i += 1; continue
        if c in "\"'":
            in_str, quote = True, c; out.append(c); i += 1; continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2; continue
        out.append(c); i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


# ─────────────────────────── 配置渲染 ───────────────────────────

def render_cliproxy_config() -> None:
    """
    用环境变量生成 config.yaml。
    ⚠️ 镜像里绝不含密钥；API key / DSN 全部由 HF Space Secrets 在运行时注入。
    """
    api_key = os.getenv("API_KEY", "").strip()
    mgmt_key = os.getenv("MANAGEMENT_KEY", "").strip() or api_key
    if not api_key:
        log("❌ 没有 API_KEY，在 HF Space Settings → Secrets 里加。退出")
        sys.exit(1)

    auth_dir = DATA_DIR / "auths"
    auth_dir.mkdir(parents=True, exist_ok=True)

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    cfg = f"""# 由 supervisor.py 在容器启动时自动生成，改这里没用 —— 改环境变量。
host: "127.0.0.1"
port: {int(os.getenv("CLIPROXY_PORT", "8080"))}

tls:
  enable: false

remote-management:
  allow-remote: true
  secret-key: {q(mgmt_key)}
  disable-control-panel: false
  panel-github-repository: "https://github.com/router-for-me/Cli-Proxy-API-Management-Center"

auth-dir: {q(str(auth_dir))}

api-keys:
  - {q(api_key)}

debug: {os.getenv("DEBUG", "false")}

pprof:
  enable: false

plugins:
  enabled: false

commercial-mode: false
logging-to-file: false
usage-statistics-enabled: true

proxy-url: {q(os.getenv("PROXY_URL", ""))}

request-retry: {int(os.getenv("REQUEST_RETRY", "3"))}
max-retry-interval: 30
"""
    target = DATA_DIR / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(cfg, encoding="utf-8")
    log(f"config.yaml 已生成 -> {target}")

    if os.getenv("PGSTORE_ENABLED", "").lower() == "true":
        if os.getenv("PGSTORE_DSN", "").strip():
            log("PGSTORE 已启用 (凭据走 Postgres)")
        else:
            log("⚠️ PGSTORE_ENABLED=true 但 PGSTORE_DSN 为空，凭据将只存本地且重启丢失")
    else:
        log("⚠️ PGSTORE 未启用，凭据只在容器内，Space 重启即丢")


# ─────────────────────────── 判活 ───────────────────────────

def check_http(url: str, timeout: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500          # 401/404 也算活着，服务在听
    except Exception:
        return False


class Proc:
    def __init__(self, spec: dict[str, Any]):
        self.id = spec["id"]
        self.command = spec["command"]
        self.cwd = spec.get("cwd") or str(APP)
        self.env_extra = spec.get("env", {})
        self.check = spec.get("check", {})
        self.grace = float(spec.get("grace", 10))
        self.delay = float(spec.get("restartDelay", 3))
        self.p: subprocess.Popen | None = None
        self.started_at = 0.0
        self.restarts = 0

    def alive(self) -> bool:
        if self.p is None or self.p.poll() is not None:
            return False
        if time.time() - self.started_at < self.grace:
            return True
        if url := self.check.get("http"):
            return check_http(url)
        return True

    def start(self) -> None:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.env_extra.items()})
        log(f"启动 {self.id}: {self.command}")
        self.p = subprocess.Popen(
            self.command, shell=True, cwd=self.cwd, env=env,
            stdout=sys.stdout, stderr=sys.stderr, start_new_session=True,
        )
        self.started_at = time.time()

    def stop(self) -> None:
        if self.p and self.p.poll() is None:
            try:
                os.killpg(os.getpgid(self.p.pid), signal.SIGTERM)
            except Exception:
                self.p.terminate()


def main() -> None:
    log("=" * 52)
    log("deepmat-gateway 容器启动")
    render_cliproxy_config()

    spec = json.loads(strip_jsonc(PROCS_FILE.read_text(encoding="utf-8")))
    procs = [Proc(s) for s in spec.get("processes", spec) if s.get("enabled", True)]

    # ⚠️ 后端读 PORT 环境变量，优先级压过 config.yaml。
    #    容器的 PORT=7860 是留给 gateway 的，必须给后端单独注入自己的端口，
    #    否则两个进程抢 7860 -> "address already in use"，后端永远起不来。
    core_port = os.getenv("CLIPROXY_PORT", "8080")
    for p in procs:
        if p.id == "core":
            p.env_extra.setdefault("PORT", core_port)
            log(f"core 端口锁定 {core_port} (gateway 独占 {os.getenv('PORT', '7860')})")

    log(f"待管理进程 {len(procs)} 个: {', '.join(p.id for p in procs)}")

    def on_sig(signum, frame):
        log(f"收到信号 {signum}，收工")
        _stop.set()

    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    for p in procs:
        p.start()
        time.sleep(float(spec.get("startStagger", 1)))

    interval = float(spec.get("checkInterval", 15))
    while not _stop.is_set():
        _stop.wait(interval)
        if _stop.is_set():
            break
        for p in procs:
            if not p.alive():
                p.restarts += 1
                log(f"⚠️ {p.id} 挂了 (第 {p.restarts} 次)，{p.delay}s 后重启")
                p.stop()
                time.sleep(p.delay)
                p.start()

    for p in procs:
        p.stop()
    log("已退出")


if __name__ == "__main__":
    main()
