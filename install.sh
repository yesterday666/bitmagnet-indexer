#!/bin/bash
# ==========================================================================
#  双库精选搜索引擎 · 一键安装脚本
#  支持 ARM64 / x86_64，自动检测架构
#  用法: bash install.sh [源路径]
#       源路径默认为当前目录。
# ==========================================================================
set -e

BOLD='\033[1m'; GREEN='\033[32m'; CYAN='\033[36m'; YELLOW='\033[33m'; RED='\033[31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
banner(){ echo -e "${CYAN}${BOLD}$1${NC}"; }

# ── 1. 确定源路径 ──
SRC="${1:-$PWD}"
SRC="$(cd "$SRC" 2>/dev/null && pwd)" || {
    SRC="$PWD"
    warn "指定路径不存在，使用当前目录: $SRC"
}
banner "安装源: $SRC"

# ── 2. 架构检测 ──
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64|armv8*)  IMG_FILE="search-engine-arm64.tar.gz"  ; PLATFORM="ARM64" ;;
    x86_64|amd64)          IMG_FILE="search-engine-amd64.tar.gz" ; PLATFORM="x86_64" ;;
    *)                     err "不支持的架构: $ARCH (需要 aarch64 或 x86_64)" ;;
esac
log "检测到架构: $PLATFORM"

# ── 3. 找镜像文件 ──
if [ -f "$SRC/$IMG_FILE" ]; then
    log "找到镜像: $IMG_FILE"
elif [ -f "$SRC/search-engine-arm64.tar.gz" ]; then
    IMG_FILE="search-engine-arm64.tar.gz"
    warn "未找到 $PLATFORM 专属镜像，使用 arm64 镜像（可能无法在当前架构运行）"
elif [ -f "$SRC/search-engine-amd64.tar.gz" ]; then
    IMG_FILE="search-engine-amd64.tar.gz"
    warn "未找到 $PLATFORM 专属镜像，使用 amd64 镜像（可能无法在当前架构运行）"
else
    err "未找到任何镜像文件 (*.tar.gz)，请确认源路径"
fi

# ── 4. 检查 docker ──
command -v docker >/dev/null 2>&1 || err "Docker 未安装，请先安装 Docker"
docker info >/dev/null 2>&1 || err "Docker 服务未运行或无权限"

# ── 5. 配置参数（交互式 + 默认值） ──
echo ""
banner "数据库连接（bitmagnet 的 PostgreSQL）"
read -p "  PG 主机地址 [127.0.0.1]: " PG_HOST;          PG_HOST="${PG_HOST:-127.0.0.1}"
read -p "  PG 端口 [5432]: " PG_PORT;                  PG_PORT="${PG_PORT:-5432}"
read -p "  PG 数据库名 [bitmagnet]: " PG_DB;            PG_DB="${PG_DB:-bitmagnet}"
read -p "  PG 用户 [bitmagnet]: " PG_USER;              PG_USER="${PG_USER:-bitmagnet}"
read -rsp "  PG 密码 [bitmagnet]: " PG_PASSWORD;          echo ""; PG_PASSWORD="${PG_PASSWORD:-bitmagnet}"

echo ""
banner "qBittorrent（推送下载用，可选）"
read -p "  qB 主机地址 [127.0.0.1]: " QB_HOST;         QB_HOST="${QB_HOST:-127.0.0.1}"
read -p "  qB 端口 [8080]: " QB_PORT;                  QB_PORT="${QB_PORT:-8080}"
read -p "  qB 用户 [admin]: " QB_USER;                  QB_USER="${QB_USER:-admin}"
read -rsp "  qB 密码 [adminadmin]: " QB_PASSWORD;          echo ""; QB_PASSWORD="${QB_PASSWORD:-adminadmin}"
read -p "  下载保存路径 [/mnt/Storage1/downloads]: " QB_SAVE; QB_SAVE="${QB_SAVE:-/mnt/Storage1/downloads}"
read -p "  分类标签 [radar]: " QB_CATEGORY;              QB_CATEGORY="${QB_CATEGORY:-radar}"

echo ""
banner "数据存储"
# 自动探测 bitmagnet 数据库所在磁盘，默认把本程序数据放同盘
DEFAULT_DATA="/DATA/AppData/search_engine_data"
for c in bitmagnet-db bitmagnet-postgres bitmagnet_db postgres bitmagnet-pg; do
    M=$(docker inspect "$c" --format '{{range .Mounts}}{{.Source}}::{{.Destination}}{{"\n"}}{{end}}' 2>/dev/null \
        | grep -iE 'postgres|pgdata|/data' | head -1 | cut -d: -f1)
    if [ -z "$M" ]; then
        M=$(docker inspect "$c" --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' 2>/dev/null | head -1)
    fi
    if [ -n "$M" ] && [ "$M" != "<no value>" ]; then
        DEFAULT_DATA="$(dirname "$M")/search_engine_data"
        warn "探测到数据库存储盘: $M"
        break
    fi
done
read -p "  Docker 数据卷映射宿主路径 [$DEFAULT_DATA]: " DATA_DIR
DATA_DIR="${DATA_DIR:-$DEFAULT_DATA}"

read -p "  面板端口 [3001]: " WEB_PORT;                  WEB_PORT="${WEB_PORT:-3001}"

# ── 6. 确认 ──
echo ""
echo -e "${CYAN}══════════════════════════════════════${NC}"
echo -e "  镜像: ${BOLD}$IMG_FILE${NC}"
echo -e "  架构: $PLATFORM"
echo -e "  数据: ${BOLD}$DATA_DIR${NC}"
echo -e "  PG:   ${BOLD}$PG_HOST:$PG_PORT/$PG_DB${NC} ($PG_USER)"
echo -e "  qB:   ${BOLD}$QB_HOST:$QB_PORT${NC} ($QB_USER)"
echo -e "  面板: http://${PG_HOST}:${WEB_PORT}"
echo -e "${CYAN}══════════════════════════════════════${NC}"
echo ""
read -p "确认安装？[Y/n]: " CONFIRM
case "$CONFIRM" in
    [Nn]*) echo "已取消"; exit 0 ;;
esac

# ── 7. 导入镜像 ──
echo ""
log "导入 Docker 镜像..."
docker load < "$SRC/$IMG_FILE" | tail -1

# ── 8. 建数据目录 ──
log "创建数据目录: $DATA_DIR"
mkdir -p "$DATA_DIR"

# ── 9. 启容器 ──
CONTAINER="search-engine"
log "停止旧容器（如果存在）..."
docker rm -f "$CONTAINER" 2>/dev/null || true

echo ""
log "启动容器..."
docker run -d \
    --name "$CONTAINER" \
    --network host \
    --restart unless-stopped \
    -v "$DATA_DIR:/data" \
    -e PG_HOST="$PG_HOST" \
    -e PG_PORT="$PG_PORT" \
    -e PG_DB="$PG_DB" \
    -e PG_USER="$PG_USER" \
    -e PG_PASSWORD="$PG_PASSWORD" \
    -e QB_HOST="$QB_HOST" \
    -e QB_PORT="$QB_PORT" \
    -e QB_USER="$QB_USER" \
    -e QB_PASSWORD="$QB_PASSWORD" \
    -e QB_SAVE_PATH="$QB_SAVE" \
    -e QB_CATEGORY="$QB_CATEGORY" \
    -e API_PORT="$WEB_PORT" \
    search-engine:latest

sleep 3
if docker ps --filter "name=$CONTAINER" --format "{{.Status}}" | grep -q Up; then
    echo ""
    banner "🎉 安装成功！"
    echo -e "  访问:  ${BOLD}http://${PG_HOST}:${WEB_PORT}${NC}"
    echo -e "  日志:  ${CYAN}docker logs -f $CONTAINER${NC}"
    echo ""
    log "首次启动会自动创建 subs schema（无需手动建表）"
    log "在「系统设置 → 订阅管理」添加关键词即可开始使用"
else
    err "容器启动失败，检查日志: docker logs $CONTAINER"
fi
