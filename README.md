# Bitmagnet-Indexer

> 🤖 纯 AI 项目 · 由 deepseek-v4-pro + OpenClaw 生成

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

##一键安装（推荐）

```bash
# 安装（自动检测最新版本）
bash <(curl -sL https://github.com/yesterday666/bitmagnet-indexer/releases/latest/download/install.sh)

# 国内镜像加速
bash <(curl -sL https://github.com/yesterday666/bitmagnet-indexer/releases/latest/download/install.sh) --mirror https://ghproxy.com

# 更新（保留配置和数据）
bash install.sh --update

# 卸载
bash install.sh --uninstall
```

> 💡 **国内镜像加速**：`--mirror` 参数支持任何 GitHub 代理服务，
> 如 `https://ghproxy.com`、`https://ghfast.com` 等。
> 原理：[镜像地址] + GitHub Release URL。

脚本会**自动**：获取最新版本 → 识别架构（ARM64/x86_64）→ 下载对应镜像 → 交互配置 → 部署。

### 卸载

```bash
# 一键卸载（停止容器 + 删除数据）
bash <(curl -sL https://github.com/yesterday666/bitmagnet-indexer/releases/latest/download/install.sh) --uninstall
```

---

## 一、手动安装

### 前置条件

| 组件 | 版本 | 说明 |
|------|------|------|
| Docker | ≥ 20.x | 推荐使用 Docker 部署 |
| PostgreSQL | ≥ 14 | bitmagnet 的数据库，需可访问 |
| bitmagnet | 任意 | 提供种子数据的大库（只读） |
| qBittorrent | 任意 | 可选，用于推送下载 |

### 从 Release 下载镜像

```bash
# ARM64
wget https://github.com/yesterday666/bitmagnet-indexer/releases/download/v1.0.0/search-engine-arm64.tar.gz

# x86_64
wget https://github.com/yesterday666/bitmagnet-indexer/releases/download/v1.0.0/search-engine-amd64.tar.gz

# 导入
docker load < search-engine-*.tar.gz
```

### 手动启动容器

```bash
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

## 二、系统环境变量

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

## 三、手动构建镜像

```bash
docker build -t search-engine:latest .
```

## 四、FAQ

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
