#!/usr/bin/env python3
"""
账号 / 面板用户文件的 Postgres 异地备份。

背景：面板自带的「异地备份」(server/services/pgsync.py) 只镜像它自己的 SQLite，
而 CodeBuddy 账号是 `/data/wb2/auths/*.json` 这种独立文件 —— 压根不在那条线上。
容器又没有持久卷，重新部署这些文件全没。

这里补上另一半：把文件也塞进同一个 PG。启动时把本地缺的拉回来，运行中定时推上去。

同步策略刻意做得保守：
  · 推送：本地文件全量 UPSERT 上去；PG 里多出来的**不删**（误删账号多半是真丢）
  · 拉取：只在本地**没有**这个文件时写回，绝不覆盖正在用的账号
          （避免把面板刚续期刷新的 token 冲回旧值）

表与面板的 16 张镜像表在同一个库但互不干扰：面板导出时只认它自己 SQLite 里有的表，
恢复时也只遍历「PG ∩ 本地 SQLite」，所以这张表既不会被它清也不会被它读。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

TABLE = "wb_file_store"
PUSH_INTERVAL = int(os.getenv("WB_AUTHSYNC_INTERVAL", "60"))

# (PG 里的路径前缀, 本地目录) —— 目录下的 *.json 全备
WATCH_DIRS: list[tuple[str, Path]] = [
    ("auths", Path(os.getenv("WB_AUTHS_DIR", "/data/wb2/auths"))),
]

# (PG 里的 key, 本地文件) —— 单个文件
WATCH_FILES: list[tuple[str, Path]] = [
    ("wbui/users.json", Path(os.getenv("WB_USERS_FILE", "/data/wbui/data/users.json"))),
]

_stop = threading.Event()
_thread: threading.Thread | None = None
_last_digest = ""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [authsync] {msg}", flush=True)


def _dsn() -> str:
    return os.getenv("WB_PGSYNC_DSN", "").strip()


def _connect():
    import psycopg  # 按需导入：没配 PG 的部署不该因为缺驱动起不来
    return psycopg.connect(_dsn(), connect_timeout=10)


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "path TEXT PRIMARY KEY, content TEXT NOT NULL, updated_at BIGINT NOT NULL)"
        )
    conn.commit()


def _resolve(pg_path: str) -> Path | None:
    """把 PG 里的逻辑路径映射回本地文件路径。不在白名单里就返回 None。"""
    for prefix, d in WATCH_DIRS:
        if pg_path.startswith(prefix + "/"):
            return d / pg_path[len(prefix) + 1:]
    for key, f in WATCH_FILES:
        if pg_path == key:
            return f
    return None


def _collect() -> dict[str, str]:
    """扫出所有要备份的文件：{PG 路径: 文本内容}"""
    out: dict[str, str] = {}
    for prefix, d in WATCH_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            if f.is_file():
                try:
                    out[f"{prefix}/{f.name}"] = f.read_text(encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    log(f"读 {f} 失败: {e}")
    for key, f in WATCH_FILES:
        if f.is_file():
            try:
                out[key] = f.read_text(encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                log(f"读 {f} 失败: {e}")
    return out


def _digest(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode())
        h.update(files[k].encode())
    return h.hexdigest()


def pull() -> int:
    """把 PG 里有、本地没有的文件补回来。返回补回数量。"""
    if not _dsn():
        return 0
    try:
        with _connect() as conn:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(f"SELECT path, content FROM {TABLE}")
                rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ 连 PG 失败，跳过拉取：{e}")
        return 0

    restored = 0
    for path, content in rows:
        target = _resolve(path)
        if target is None or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            restored += 1
            log(f"已拉回 {path}")
        except Exception as e:  # noqa: BLE001
            log(f"写 {target} 失败: {e}")
    if restored:
        log(f"共拉回 {restored} 个文件")
    return restored


def push() -> int:
    """把本地文件全量推上去（内容没变就跳过）。返回推送数量。"""
    global _last_digest
    if not _dsn():
        return 0
    files = _collect()
    if not files:
        return 0
    d = _digest(files)
    if d == _last_digest:
        return 0
    try:
        with _connect() as conn:
            _ensure_table(conn)
            now = int(time.time())
            with conn.cursor() as cur:
                for path, content in files.items():
                    cur.execute(
                        f"INSERT INTO {TABLE} (path, content, updated_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content, "
                        "updated_at = EXCLUDED.updated_at",
                        (path, content, now),
                    )
            conn.commit()
        _last_digest = d
        n_acc = sum(1 for k in files if k.startswith("auths/"))
        log(f"已推送 {len(files)} 个文件（账号 {n_acc} 个）")
        return len(files)
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ 推送失败：{e}")
        return 0


def _loop() -> None:
    while not _stop.wait(PUSH_INTERVAL):
        try:
            push()
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ 同步线程异常：{e}")


def start_background() -> None:
    """进容器时调一次：先落一次盘，再挂上定时线程。"""
    global _thread
    if not _dsn():
        return
    push()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="authsync", daemon=True)
        _thread.start()
    log(f"后台同步已启动（每 {PUSH_INTERVAL}s 一次）")


def stop() -> None:
    _stop.set()
