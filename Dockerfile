FROM python:3.11-slim

LABEL maintainer="MengStar-L"
LABEL description="Aria2TelDrive - aria2 下载 + TelDrive 上传中转服务"

# 安装 aria2 和 supervisor
RUN apt-get update && \
    apt-get install -y --no-install-recommends aria2 supervisor && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/Aria2TelDrive

# 先复制依赖文件，利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ app/
COPY config.example.toml .

# 复制 supervisor 配置
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 创建数据和下载目录
RUN mkdir -p /data /downloads

# 环境变量
ENV CONFIG_PATH=/data/config.toml
ENV DOCKER=1

EXPOSE 8010

VOLUME ["/data", "/downloads"]

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
