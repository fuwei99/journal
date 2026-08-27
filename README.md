<div align="center">

# 📔 Journal

**自己托管的轻量日记 / 手账站点。打开就写，写完即存。**

![python](https://img.shields.io/badge/python-3.12-blue)
![docker](https://img.shields.io/badge/docker-ready-2496ED)
![license](https://img.shields.io/badge/license-MIT-green)

</div>

---

## 关于

市面上的日记 App，要么强制登录云账号，要么把你的内容存在别人的服务器上。
这个项目的想法很简单——**日记是私事，应该跑在自己的地方。**

一个容器，一个端口，浏览器打开就能写。内容存自己的 Postgres，
换机器只要把连接串带走，所有条目自动回来。

## 特点

- **📝 手账面板** — 内置编辑界面，支持 Markdown、标签、按日期归档
- **🗄️ 自带数据** — 存储层走 Postgres，容器无状态，随便重建
- **🔌 单端口** — 内部多个组件，对外只开一个端口，方便丢在任何 PaaS 上
- **🧩 可扩展** — 想加个图床、加个 RSS 输出？改一行路由表，热生效
- **🔐 无内置密钥** — 镜像里不含任何凭据，全部运行时注入

## 快速开始

```bash
docker run -d --name journal -p 7860:7860 \
  -e API_KEY='给自己设一个访问口令' \
  -e PGSTORE_ENABLED=true \
  -e PGSTORE_DSN='postgresql://user:pass@host:5432/postgres?sslmode=require' \
  ghcr.io/<your-name>/journal:latest
```

打开 `http://localhost:7860/admin` 开始写。

> 不填 `PGSTORE_*` 也能跑，但内容只存在容器里，删容器就没了。

## 配置

全部通过环境变量，**不用改代码**：

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `API_KEY` | ✅ | — | 访问口令 |
| `MANAGEMENT_KEY` | | 同 `API_KEY` | 手账面板单独口令 |
| `PORT` | | `7860` | 对外端口 |
| `PGSTORE_ENABLED` | | `false` | 是否启用 Postgres 存储 |
| `PGSTORE_DSN` | | — | Postgres 连接串 |
| `PGSTORE_SCHEMA` | | `public` | schema 名 |
| `PROXY_URL` | | — | 出网代理（需要时填） |
| `DEBUG` | | `false` | 详细日志 |

## 部署到 PaaS

已经打好镜像了，任何支持 Docker 的平台一行就能跑：

```dockerfile
FROM ghcr.io/<your-name>/journal:latest
```

口令和数据库连接串在平台的 Secrets 面板里填，**不要写进仓库**。

## 架构

```
                 ┌─────────── 7860 (唯一对外端口) ───────────┐
   浏览器 ──────▶ │  gateway.py   按 URL 前缀分发 / 流式转发   │
                 └───────────────────┬──────────────────────┘
                                     │
                            ┌────────▼────────┐
                            │  journal-core   │  内容渲染与存取
                            │   (127.0.0.1)   │
                            └────────┬────────┘
                                     │
                              ┌──────▼──────┐
                              │  Postgres   │
                              └─────────────┘

   supervisor.py  = PID 1，渲染配置 → 拉起上面两个进程 → 15s 巡检自动重启
```

### 为什么要套一层 gateway

免费 PaaS 通常只给一个端口。gateway 让内部想跑几个组件就跑几个，
对外还是一个入口。顺带解决三件事：

- **流式不缓冲** — 逐块转发，长文本编辑的实时预览不会卡成一坨
- **WebSocket 直通** — 编辑器的实时保存需要
- **路由热加载** — 加服务不用重启，配置写坏了自动保留上一份

## 加一个新组件

编辑 `app/routes.jsonc`，在兜底路由 `/` **之前**插一条，保存即生效：

```jsonc
{
  "id": "gallery",
  "prefix": "/gallery",
  "target": "http://127.0.0.1:9001",
  "strip_prefix": true,
  "health": "http://127.0.0.1:9001/health",
  "enabled": true
}
```

要它跟容器一起被拉起、挂了自动重启，再往 `app/processes.jsonc` 加一条同名进程。

查看当前路由与后端状态：

```bash
curl http://localhost:7860/__index
```

> ⚠️ 兜底路由 `/` 不要删。面板 HTML 里的 `/assets/*.js` 是绝对路径，
> 靠它直落后端，删了会白屏。

## 项目结构

```
├── Dockerfile                    # 镜像定义（不含任何密钥）
├── requirements.txt
├── app/
│   ├── supervisor.py             # PID 1：渲染配置 + 进程保活
│   ├── gateway.py                # 单端口反向代理
│   ├── routes.jsonc              # 路由表（热加载）
│   └── processes.jsonc           # 进程表
└── .github/workflows/build.yml   # CI：构建并推送镜像
```

## 端点

| 路径 | 说明 |
|---|---|
| `/admin` | 手账编辑面板 |
| `/__health` | gateway 健康检查 |
| `/__index` | 路由表 + 各后端存活状态 |

## 开发

```bash
pip install -r requirements.txt
API_KEY=test python3 app/supervisor.py
```

改 `routes.jsonc` 不用重启；改 `processes.jsonc` 要重启。

## License

MIT
