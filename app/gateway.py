#!/usr/bin/env python3
"""
Path Gateway —— 单端口多服务反向代理。

对外只暴露 7860（HF Space 唯一开放端口），按 URL 前缀反代到容器内各服务。
从 k40 的 ~/path-gateway 移植，去掉 Termux 相关部分，改为容器内启动。

设计要点:
  - 全流式: SSE / chunked 不缓冲，逐块转发（LLM 场景必需）
  - WebSocket 自动转发
  - 长前缀优先匹配，'/' 兜底垫底（面板 /assets/*.js 是绝对路径，靠它直落后端）
  - 路由表热加载，写坏了保留上一份有效配置，不炸服务
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

LOG = logging.getLogger("gateway")

ROUTES_FILE = Path(os.getenv("ROUTES_FILE", "/app/routes.jsonc"))
# GATEWAY_PORT 优先：容器里 PORT 可能被 PaaS 注入，也被后端二进制争用
LISTEN_PORT = int(os.getenv("GATEWAY_PORT") or os.getenv("PORT") or "7860")

# 逐块转发，别攒着。攒了 SSE 就废了
CHUNK = 8192
# 上游超时: connect 短一点快速失败，read 给足（推理模型能想几分钟）
TIMEOUT = httpx.Timeout(
    connect=float(os.getenv("UPSTREAM_CONNECT_TIMEOUT", "10")),
    read=float(os.getenv("UPSTREAM_READ_TIMEOUT", "900")),
    write=60.0,
    pool=10.0,
)

# 逐跳头，不能透传给上游/客户端
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def strip_jsonc(text: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串内的内容。"""
    out, i, n = [], 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str, quote = True, c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(c)
        i += 1
    # 去掉尾逗号
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class RouteTable:
    """路由表 + 热加载。配置写坏时保留上一份有效的。"""

    def __init__(self, path: Path, eager: bool = False):
        self.path = path
        self.routes: list[dict[str, Any]] = []
        self._mtime = 0.0
        # 默认不在 import 期读盘：文件可能还没挂上来，也方便测试
        if eager:
            self.load(initial=True)

    def load(self, initial: bool = False) -> bool:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(strip_jsonc(raw))
            items = data.get("routes", data) if isinstance(data, dict) else data
            parsed = []
            for r in items:
                if not r.get("enabled", True):
                    continue
                prefix = "/" + r["prefix"].strip("/") if r.get("prefix", "/") != "/" else "/"
                parsed.append({
                    "id": r.get("id") or prefix,
                    "prefix": prefix,
                    "target": r["target"].rstrip("/"),
                    "strip_prefix": r.get("strip_prefix", True),
                    "rewrite": r.get("rewrite", ""),
                    "headers": r.get("headers", {}),
                    "strip_headers": [h.lower() for h in r.get("strip_headers", [])],
                    "health": r.get("health", ""),
                    "note": r.get("note", ""),
                })
            # 长前缀优先，'/' 垫底
            parsed.sort(key=lambda x: (x["prefix"] == "/", -len(x["prefix"])))
            self.routes = parsed
            self._mtime = self.path.stat().st_mtime
            desc = ", ".join(f"{r['prefix']}→{r['target']}" for r in parsed)
            LOG.info("路由表已加载: %d 条 -> %s", len(parsed), desc)
            return True
        except Exception as e:
            LOG.error("路由表加载失败(%s): %s", "启动" if initial else "热加载", e)
            if initial:
                self.routes = []
            else:
                LOG.error("→ 保留上一份有效路由 (%d 条)", len(self.routes))
            return False

    def maybe_reload(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            self._mtime = m
            self.load()

    def match(self, path: str) -> tuple[dict[str, Any] | None, str]:
        for r in self.routes:
            p = r["prefix"]
            if p == "/":
                return r, path
            if path == p or path.startswith(p + "/"):
                if r["rewrite"]:
                    return r, r["rewrite"]
                if r["strip_prefix"]:
                    return r, path[len(p):] or "/"
                return r, path
        return None, path


TABLE = RouteTable(ROUTES_FILE)
CLIENT: httpx.AsyncClient | None = None


def build_headers(req_headers, route: dict[str, Any], target: str) -> dict[str, str]:
    h = {}
    for k, v in req_headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in route["strip_headers"]:
            continue
        if lk == "host":
            continue
        h[k] = v
    # Host 用上游的，免得后端按 Host 做路由时错乱
    h["Host"] = target.split("://", 1)[-1]
    h.update(route["headers"])
    return h


async def proxy(request: Request) -> Response:
    TABLE.maybe_reload()
    path = "/" + request.path_params.get("path", "")
    route, upstream_path = TABLE.match(path)
    if route is None:
        return JSONResponse({"error": f"no route for {path}"}, status_code=404)

    url = route["target"] + upstream_path
    if request.url.query:
        url += "?" + request.url.query

    assert CLIENT is not None
    try:
        req = CLIENT.build_request(
            request.method,
            url,
            headers=build_headers(request.headers, route, route["target"]),
            content=request.stream(),
        )
        resp = await CLIENT.send(req, stream=True)
    except Exception as e:
        LOG.warning("✗ %s %s -> %s : %s: %s", request.method, path, url, type(e).__name__, e)
        return JSONResponse(
            {"error": "bad gateway", "detail": f"{type(e).__name__}: {e}", "upstream": url},
            status_code=502,
        )

    out = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    out.pop("content-length", None)

    async def body():
        try:
            async for chunk in resp.aiter_raw(CHUNK):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body(),
        status_code=resp.status_code,
        headers=out,
        background=BackgroundTask(resp.aclose),
    )


async def ws_proxy(ws: WebSocket) -> None:
    """WebSocket 双向转发。"""
    try:
        import websockets
    except ImportError:
        await ws.close(code=1011)
        return

    TABLE.maybe_reload()
    path = "/" + ws.path_params.get("path", "")
    route, upstream_path = TABLE.match(path)
    if route is None:
        await ws.close(code=1008)
        return

    url = route["target"].replace("http://", "ws://").replace("https://", "wss://") + upstream_path
    if ws.url.query:
        url += "?" + ws.url.query

    await ws.accept()
    try:
        async with websockets.connect(url, open_timeout=15, max_size=None) as up:
            async def c2u():
                try:
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            break
                        if (d := m.get("text")) is not None:
                            await up.send(d)
                        elif (d := m.get("bytes")) is not None:
                            await up.send(d)
                except (WebSocketDisconnect, RuntimeError):
                    pass

            async def u2c():
                try:
                    async for m in up:
                        if isinstance(m, bytes):
                            await ws.send_bytes(m)
                        else:
                            await ws.send_text(m)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                {asyncio.create_task(c2u()), asyncio.create_task(u2c())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as e:
        LOG.warning("✗ WS %s -> %s : %s", path, url, e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def index(request: Request) -> Response:
    """路由索引 + 后端存活。'/' 被兜底占了，所以走 /__index。"""
    TABLE.maybe_reload()
    assert CLIENT is not None
    items = []
    for r in TABLE.routes:
        alive = None
        if r["health"]:
            try:
                rr = await CLIENT.get(r["health"], timeout=5.0)
                alive = rr.status_code < 500
            except Exception:
                alive = False
        items.append({
            "id": r["id"], "prefix": r["prefix"], "target": r["target"],
            "strip_prefix": r["strip_prefix"], "note": r["note"], "alive": alive,
        })
    return JSONResponse({
        "service": "deepmat-gateway",
        "port": LISTEN_PORT,
        "routes_file": str(ROUTES_FILE),
        "count": len(items),
        "routes": items,
    })


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True, "routes": len(TABLE.routes), "ts": int(time.time())})


@contextlib.asynccontextmanager
async def lifespan(app_: Starlette):
    global CLIENT
    CLIENT = httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    TABLE.load(initial=True)
    LOG.info("Gateway 起来了, 端口 %s, 路由 %s", LISTEN_PORT, ROUTES_FILE)
    try:
        yield
    finally:
        await CLIENT.aclose()


app = Starlette(
    routes=[
        Route("/__health", health, methods=["GET"]),
        Route("/__index", index, methods=["GET"]),
        WebSocketRoute("/{path:path}", ws_proxy),
        Route("/{path:path}", proxy,
              methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    import uvicorn

    uvicorn.run(
        app, host="0.0.0.0", port=LISTEN_PORT,
        log_level="warning", access_log=False, timeout_keep_alive=75,
    )
