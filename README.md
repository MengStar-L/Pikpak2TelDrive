# Aria2TelDrive

aria2 下载 + TelDrive 上传中转服务 —— 通过 Web 面板管理下载任务，自动上传到 TelDrive，支持实时进度监控。

## 功能特性

- 📥 **aria2 下载**：通过 aria2 RPC 接口下载文件，支持暂停/恢复/重试
- 📤 **自动上传**：下载完成后自动分片上传到 TelDrive，支持文件夹结构保留
- 🌐 **Web 管理面板**：可视化任务管理，实时进度显示
- 📊 **WebSocket 推送**：实时同步下载/上传进度到前端
- 🗑️ **自动清理**：上传完成后可自动删除本地文件
- 💾 **磁盘空间限流**：设置磁盘使用上限，达到 90% 时自动限制下载并发数，空间降至 60% 后逐步恢复
- 📈 **仪表盘监控**：实时显示磁盘使用量、CPU 使用率、下载/上传速度等系统状态
- 🧠 **CPU 自适应限速**：根据系统 CPU 使用率自动限制下载速度，CPU 恢复后逐步解除限速
- 🔄 **上传并发动态调整**：修改上传并发数后立即生效，无需重启，支持热更新
- 🧩 **Random Chunking 支持**：兼容 TelDrive Random Chunking 模式
- ♻️ **自动重试**：下载/上传失败自动重试，支持手动一键重试
- 🧹 **批量管理**：支持一键清除已完成/失败任务

## 部署步骤

### 方式一：Docker 部署（推荐）

#### 使用 docker-compose

1. 下载配置文件：

```bash
mkdir aria2teldrive && cd aria2teldrive
wget https://raw.githubusercontent.com/MengStar-L/Aria2TelDrive/main/config.example.toml -O config.toml
wget https://raw.githubusercontent.com/MengStar-L/Aria2TelDrive/main/docker-compose.yml
```

2. 编辑 `config.toml` 填入你的信息（或直接在 `docker-compose.yml` 中通过环境变量配置）

3. 启动服务：

```bash
docker-compose up -d
```

#### 使用 docker run

```bash
docker run -d \
  --name aria2teldrive \
  --restart unless-stopped \
  -p 8010:8010 \
  -v $(pwd)/data:/data \
  -v $(pwd)/downloads:/downloads \
  -e TZ=Asia/Shanghai \
  mengstarl/aria2teldrive:latest
```

#### 卷映射说明

| 容器路径 | 说明 | 建议 |
|---------|------|------|
| `/data` | 配置和数据目录，包含 `config.toml` 和 `tasks.db` | **必须映射**，确保配置和任务记录持久化 |
| `/downloads` | 下载文件临时存放目录 | **必须映射**，确保下载文件可访问 |

> **提示**：映射 `/data` 目录后，首次启动会自动生成默认 `config.toml`，编辑后重启容器即可。也可以直接将 `config.toml` 放入映射的 `data` 目录。

#### 环境变量配置

所有配置项均支持通过环境变量覆盖，格式为 `SECTION_KEY` 全大写。优先级：**环境变量 > config.toml > 默认值**。

完整变量列表见 [docker-compose.yml](docker-compose.yml) 中的注释，常用变量：

```bash
TELDRIVE_API_HOST=http://your-teldrive:7888
TELDRIVE_ACCESS_TOKEN=your_jwt_token
TELDRIVE_CHANNEL_ID=123456
ARIA2_MAX_CONCURRENT=3
GENERAL_AUTO_DELETE=true
```

> **注意**：Docker 镜像已内置 aria2，无需单独安装。`config.toml` 中 aria2 RPC 地址保持默认 `http://localhost:6800` 即可。

---

### 方式二：手动部署

#### 1. 下载项目

```bash
git clone https://github.com/MengStar-L/Aria2TelDrive.git /opt/Aria2TelDrive
```

#### 2. 创建虚拟环境并安装依赖

```bash
cd /opt/Aria2TelDrive
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 3. 创建配置文件

```bash
cp config.example.toml config.toml
```

编辑配置文件：

```bash
nano config.toml
```

填入你的信息：

```toml
[server]
port = 8010                         # Web 管理面板端口

[aria2]
rpc_url = "http://localhost"        # aria2 RPC 地址
rpc_port = 6800                     # aria2 RPC 端口
rpc_secret = ""                     # aria2 RPC 密钥
max_concurrent = 3                  # 最大同时下载数
download_dir = "./downloads"        # 下载目录

[teldrive]
api_host = "http://localhost:7888"  # TelDrive API 地址
access_token = ""                   # TelDrive JWT Token
channel_id = 0                      # Telegram 频道 ID
chunk_size = "500M"                 # 分片大小 (支持 M/G 后缀)
upload_concurrency = 4              # 上传并发数 (支持热更新)
upload_dir = ""                     # 上传文件路径 (留空使用下载目录)
target_path = "/"                   # TelDrive 目标路径

[general]
max_retries = 3                     # 失败重试次数
auto_delete = true                  # 上传后自动删除本地文件
max_disk_usage = 0                  # 磁盘使用上限(GB)，达90%限制并发，降至60%恢复，0=不限制
cpu_limit = 85                      # CPU 使用率上限(%)，超过时限制下载速度，0=不限制
```

#### 4. 确保 aria2 已运行

本程序通过 RPC 连接外部 aria2 实例，请确保 aria2 已启动并开启 RPC：

```bash
aria2c --enable-rpc --rpc-listen-all=true --rpc-listen-port=6800
```

#### 5. 运行

```bash
source /opt/Aria2TelDrive/venv/bin/activate
cd /opt/Aria2TelDrive
python app/main.py
```

访问 `http://localhost:8010` 即可打开管理面板。

#### 6. 注册为系统服务（可选）

复制项目中的服务文件：

```bash
cp /opt/Aria2TelDrive/aria2teldrive.service /etc/systemd/system/
```

启用并启动服务：

```bash
systemctl daemon-reload
systemctl enable --now aria2teldrive
```

#### 7. 确认运行状态

```bash
systemctl status aria2teldrive
```

看到 `active (running)` 即表示部署成功 ✅

## 更新 / 重新安装

当需要更新到最新版本时，执行以下步骤：

```bash
# 1. 进入项目目录
cd /opt/Aria2TelDrive

# 2. 拉取最新代码
git pull

# 3. 激活虚拟环境并更新依赖
source venv/bin/activate
pip install -r requirements.txt

# 4. 重启服务
systemctl restart aria2teldrive
```

如果需要完全重新安装（例如 Python 版本变更或环境损坏）：

```bash
cd /opt/Aria2TelDrive

# 删除旧虚拟环境
rm -rf venv

# 重新创建并安装
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 重启服务
systemctl restart aria2teldrive
```

> **注意**：`config.toml` 和 `tasks.db` 不会被 `git pull` 覆盖，配置和任务记录会保留。

## 常用命令

```bash
# 查看实时日志
journalctl -u aria2teldrive -f

# 重启服务
systemctl restart aria2teldrive

# 停止服务
systemctl stop aria2teldrive
```

## License

MIT
