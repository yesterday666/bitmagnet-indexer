FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /data/logs

# 复制代码
COPY *.py panel.html ./

# 数据卷
VOLUME ["/data"]

EXPOSE 3001

ENV PYTHONUNBUFFERED=1

# 生产模式用 gunicorn
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:3001", "--timeout", "120", "api_server:app"]
