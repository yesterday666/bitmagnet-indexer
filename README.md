# 纯ai项目由deepseek-v4-pro生成
# 双库精选搜索引擎 (Bitmagnet-Indexer)

一个"外挂"式种子搜索/订阅/下载系统，接入现有 **bitmagnet** 爬虫的 PostgreSQL 数据库。
采用**双库架构**：bitmagnet 大库只读、永不修改；本程序在同实例创建 `subs` schema 作为**精选小库**
（全量 GIN 索引，毫秒级搜索）。启动**自动建表**，开箱即用。

- 内置 Web 面板：`http://<设备IP>:3001`
- 镜像文件：`search-engine-arm64.tar.gz`（ARM64） / `search-engine-amd64.tar.gz`（x86_64）

---

## 功能特性

- 🔍 **四种搜索模式**：智能搜索（自动判断番号/关键词）、番号搜索（纯番号正则）、模糊搜索（子串匹配）、精确搜索（整词边界）
- ⏱ **时间筛选**：最近 1 天 / 7 天 / 31 天 / 1 年
- 📏 **大小筛选**：按文件大小范围过滤
- 📋 **订阅管理**：关键词订阅 + 全量回溯 + 增量监控
- ⬇️ **qBittorrent 推送**：监控大库新增，命中即推
- 🖼️ **多源封面**：DMM / AWS 镜像 / PS 等多 CDN 自动回退

---

## 一键安装（推荐）

可以直接一行命令在线安装：

```
bash <(curl -sL https://github.com/yesterday666/bitmagnet-indexer/releases/download/v1.0.0/install.sh)
```
## 一键卸载
bash install.sh --uninstall
```
脚本会**自动**：识别架构（ARM64/x86_64）选对镜像 → 探测 bitmagnet 数据库所在磁盘并默认把数据卷放同盘
（也可手动输入路径）→ 交互填写 PG / qBittorrent 连接 → 导入镜像 → 启动容器。装完访问 `http://<设备IP>:端口`。

> 也可指定源路径：`bash install.sh /path/to/source`

下面是手动部署步骤（不想用脚本时参考）。

---

## 一、前置条件

| 组件 | 版本 | 说明 |
|------|------|------|
| Docker | ≥ 20.x | 推荐使用 Docker 部署 |
| PostgreSQL | ≥ 14 | bitmagnet 的数据库，需可访问 |
| bitmagnet | 任意 | 提供种子数据的大库（只读） |
| qBittorrent | 任意 | 可选，用于推送下载 |

## 二、镜像获取

预构建镜像文件（约 65MB）：

- `search-engine-arm64.tar.gz` — ARM64 架构（树莓派、NAS、Armbian）
- `search-engine-amd64.tar.gz` — x86_64 架构

## 三、导入镜像并启动容器

```bash
# 1. 导入镜像
docker load < search-engine-arm64.tar.gz

# 2. 创建数据目录
mkdir -p /mnt/Storage1/search_engine_data

# 3. 启动容器
docker run -d \
    --name search-engine \
    --network host \
    --restart unless-stopped \
    -v /mnt/Storage1/search_engine_data:/data \
    -e PG_HOST=127.0.0.1 \
    -e PG_PORT=5432 \
    -e PG_DB=bitmagnet \
    -e PG_USER=bitmagnet \
    -e PG_PASSWORD=bitmagnet \
    -e QB_HOST=127.0.0.1 \
    -e QB_PORT=8080 \
    -e QB_USER=admin \
    -e QB_PASSWORD=adminadmin \
    -e QB_SAVE_PATH=/mnt/Storage1/downloads \
    -e QB_CATEGORY=radar \
    -e API_PORT=3001 \
    search-engine:latest
```

## 四、系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PG_HOST` | `127.0.0.1` | PostgreSQL 主机 |
| `PG_PORT` | `5432` | PostgreSQL 端口 |
| `PG_DB` | `bitmagnet` | 数据库名 |
| `PG_USER` | `bitmagnet` | 数据库用户 |
| `PG_PASSWORD` | `bitmagnet` | 数据库密码 |
| `QB_HOST` | `127.0.0.1` | qBittorrent 主机（留空禁用推送） |
| `QB_PORT` | `8080` | qBittorrent 端口 |
| `QB_USER` | `admin` | qBittorrent 用户 |
| `QB_PASSWORD` | `adminadmin` | qBittorrent 密码 |
| `QB_SAVE_PATH` | `/mnt/Storage1/downloads` | qBittorrent 保存路径 |
| `QB_CATEGORY` | `radar` | qBittorrent 分类标签 |
| `API_PORT` | `3001` | Web 面板端口 |

## 五、手动部署（Docker 方式）

```bash
# 构建镜像
docker build -t search-engine:latest .

# 创建数据目录
mkdir -p /mnt/Storage1/search_engine_data

# 启动
docker run -d \
    --name search-engine \
    --network host \
    --restart unless-stopped \
    -v /mnt/Storage1/search_engine_data:/data \
    -e PG_HOST=127.0.0.1 \
    -e PG_PORT=5432 \
    -e PG_DB=bitmagnet \
    -e PG_USER=bitmagnet \
    -e PG_PASSWORD=bitmagnet \
    search-engine:latest
```

## 六、手动部署（Systemd 方式，裸机运行）

```bash
# 1. 安装系统依赖
apt install python3 python3-pip python3-venv postgresql-client

# 2. 创建目录
mkdir -p /DATA/AppData/search_engine /data/search_engine_data
cp *.py panel.html Dockerfile /DATA/AppData/search_engine/

# 3. 虚拟环境
cd /DATA/AppData/search_engine
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. 创建 systemd 服务
cat > /etc/systemd/system/search-engine.service << 'EOF'
[Unit]
Description=Bitmagnet-Indexer Search Engine
After=network.target

[Service]
Type=simple
WorkingDirectory=/DATA/AppData/search_engine
ExecStart=/DATA/AppData/search_engine/venv/bin/python /DATA/AppData/search_engine/api_server.py
Restart=on-failure
RestartSec=5
Environment=SQLITE_PATH=/data/search_engine_data/search_engine.db
Environment=LOG_DIR=/data/search_engine_data/logs
Environment=PG_HOST=127.0.0.1
Environment=PG_PORT=5432
Environment=PG_DB=bitmagnet
Environment=PG_USER=bitmagnet
Environment=PG_PASSWORD=bitmagnet

[Install]
WantedBy=multi-user.target
EOF

# 5. 启动
systemctl enable --now search-engine.service
```

## 七、界面截图

访问 `http://<IP>:3001` 即可打开 Web 面板：

- **搜索**：Bing 风格搜索栏 + 排序/时间/大小/模式筛选
- **订阅管理**：关键词订阅、qB 推送开关
- **智能番号识别**：自动适配 SSIS-790 / FC2-PPV-1234567 / 1PONDO 等格式

## 八、FAQ

**Q: 搜索不出结果？**  
A: 首次使用小库为空，需要先添加订阅触发全量回溯。在「系统设置 → 订阅管理」添加关键词后，后台自动开始回溯 bitmagnet 大库并灌入小库，此过程可能需要几分钟到几小时（取决于数据量）。

**Q: Failed to fetch？**  
A: 如果面板访问正常但 API 请求失败，检查面板所在页面 URL 的端口是否与 API 端口一致（`--network host` 时默认 3001）。

**Q: PG 连接失败？**  
A: 确认 PG_USER 不一定是 `bitmagnet`，可能是 `postgres` 或其他用户。连接时试试 `psql -U postgres -d bitmagnet`。

**Q: qB 推送不生效？**  
A: 在 Web 面板「系统设置」中确认 qB 连接状态为绿色已连接，且订阅的 ⬇️ 开关已点亮。

**Q: 爬虫每分钟新增速率为 0？**  
A: 如果 bitmagnet 刚启动或 DHT 网络连接不稳定，新增采集速率为 0 属于正常现象。不影响已有数据的搜索与推送。

## 许可证

MIT License
