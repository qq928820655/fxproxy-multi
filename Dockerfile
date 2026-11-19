FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FXPROXY_HOST=0.0.0.0

WORKDIR /app

# 只装运行依赖；登录库/浏览器仅在住宅 IP 首次取凭证时需要，不进镜像。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY fxproxy/ ./fxproxy/
COPY deploy/container_entry.py ./deploy/container_entry.py

# HTTP 代理 / SOCKS5 / 控制面板
EXPOSE 8080 1080 8765

# 凭证/监听/订阅地址全部通过 FXPROXY_* 环境变量注入（config.py 直接读 env），
# 不写 config.json，镜像里没有任何凭证。
CMD ["python", "deploy/container_entry.py"]
