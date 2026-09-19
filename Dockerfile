# ============================================================
#  deepmat-gateway
#
#  单端口(7860)多服务网关。
#
#  ⚠️ 关键设计: 上游二进制在 **CI 构建期** 由 GitHub Actions 下载并放进
#     build context 的 vendor/ 目录，这里只做 COPY。
#     Dockerfile 内**不出现任何上游仓库名/下载地址**，
#     也不 FROM 任何第三方 API 代理镜像 —— 成品镜像看不出来源。
#
#  镜像内不含任何密钥；API key / DSN 全部运行时由环境变量注入。
# ============================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_PORT=8080 \
    CLIPROXY_PORT=7860 \
    DATA_DIR=/data \
    HOME=/data

# ⚠️ 端口分配（别再改回来）:
#      core    -> 7860   后端二进制在 cloud 模式下铁了心要 7860，
#                        config.yaml 的 port 和 PORT 环境变量都压不住它，
#                        与其硬碰不如避让。
#      gateway -> 8080   唯一对外监听，PaaS 的 app_port 填 8080。
#    不要设 ENV PORT：那个名字后端会抢，会导致两进程撞同一端口。

# ca-certificates 必须装: 后端是 Go 程序，crypto/x509 只认标准路径的 CA，
# 缺了会报 x509: certificate signed by unknown authority
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# CI 预先放好的后端二进制（构建期注入，见 .github/workflows/build.yml）
# 落地名刻意中性: 镜像层里不出现上游项目名
COPY vendor/backend /opt/bin/journal-core
RUN chmod +x /opt/bin/journal-core

# 第二后端：OpenAI 兼容网关（CI 构建期产出，Dockerfile 里只见中性文件名）
COPY vendor/wb2-core /opt/bin/wb2-core
COPY vendor/wb2-config.example.json /opt/wb2/config.example.json
COPY vendor/wb2-tools/ /opt/wb2/
RUN chmod +x /opt/bin/wb2-core \
 && chmod +x /opt/wb2/*.sh 2>/dev/null || true

COPY app/ /app/

# HF Space 以任意 UID 运行容器，/data 必须对所有人可写
RUN mkdir -p /data/auths && chmod -R 777 /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/__health || exit 1

# PID 1 是 supervisor: 渲染配置 -> 拉起后端 + 网关 -> 保活
CMD ["python3", "/app/supervisor.py"]
