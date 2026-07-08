FROM python:3.12-slim

# ── pg_dump（小库备份用）──
# Debian trixie 自带 postgresql-client=17，pg_dump 17 可正常导出 PG16 服务端
# （规则：pg_dump 版本需 >= 服务端版本）。若换更旧基础镜像(bookworm=15)需改用 PGDG 装 client-16。
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /data/logs

COPY *.py panel.html ./

VOLUME ["/data"]
EXPOSE 3001

ENV PYTHONUNBUFFERED=1 \
    SQLITE_PATH=/data/search_engine.db \
    LOG_DIR=/data/logs

CMD ["python", "api_server.py"]
