FROM python:3.11-slim

# OCI 元数据：标注版本与报告封存能力（canonical JSON + SHA-256）。
LABEL org.opencontainers.image.title="coldchain-audit-api" \
      org.opencontainers.image.version="1.1.0" \
      org.opencontainers.image.description="冷链温度合规审计 API（含报告封存摘要与校验）"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COLDCHAIN_DB_PATH=/data/coldchain.db

WORKDIR /app

# 先装依赖，利用镜像层缓存。封存摘要仅使用标准库（json/hashlib），无新增第三方依赖。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

COPY app ./app

USER appuser

EXPOSE 8000

# 容器启动即完成数据库初始化（lifespan 中幂等建表），无需外部账号。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
