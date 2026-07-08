#!/bin/bash
# ==========================================================================
#  Bitmagnet-Indexer · 一键安装 / 卸载脚本
#  支持从 GitHub Release 自动下载对应架构的 Docker 镜像并部署
#  用法:
#    安装: bash <(curl -sL https://github.com/yesterday666/bitmagnet-indexer/releases/download/v1.0.0/install.sh)
#         bash install.sh --mirror https://ghproxy.com   # 国内镜像加速
#    卸载: bash install.sh --uninstall
# ==========================================================================
set -e

BOLD='\033[1m'; GREEN='\033[32m'; CYAN='\033[36m'; YELLOW='\033[33m'; RED='\033[31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
banner(){ echo -e "${CYAN}${BOLD}$1${NC}"; }

CONTAINER="search-engine"
MIRROR=""
DATA_DIR_DEFAULT="/mnt/Storage1/search_engine_data"

# ── 解析参数 ──
while [ $# -gt 0 ]; do
    case "$1" in
        --mirror) MIRROR="$2"; shift 2 ;;
        --mirror=*) MIRROR="${1#*=}"; shift ;;
        --uninstall|-u) UNINSTALL=1; shift ;;
        --help|-h) HELP=1; shift ;;
        *) shift ;;
    esac
done

# ── 镜像加速 ──
RELEASE_BASE="https://github.com/yesterday666/bitmagnet-indexer/releases/download/v1.0.0"
if [ -n "$MIRROR" ]; then
    MIRROR="${MIRROR%/}"
    RELEASE_URL="${MIRROR}/${RELEASE_BASE#https://}"
    log "使用镜像加速: $MIRROR"
else
    RELEASE_URL="$RELEASE_BASE"
fi

# ── 卸载模式 ──
if [ "${UNINSTALL:-0}" = "1" ]; then
    echo ""
    banner "🗑️  卸载 Bitmagnet-Indexer"
    echo ""
    read -p "确认卸载？将停止并删除容器，数据目录可选保留。 [y/N]: " CONFIRM
    case "$CONFIRM" in
        [Yy]*|"yes"|"YES") ;;
        *) echo "已取消"; exit 0 ;;
    esac
    echo ""
    log "停止并删除容器..."
    docker rm -f "$CONTAINER" 2>/dev/null && log "容器已删除" || warn "容器不存在"
    echo ""
    read -p "是否删除数据目录（含 SQLite 数据库、日志）？ [y/N]: " DEL_DATA
    case "$DEL_DATA" in
        [Yy]*|"yes"|"YES")
            for d in "$DATA_DIR_DEFAULT" "/DATA/AppData/search_engine_data" "/data/search_engine_data"; do
                [ -d "$d" ] && rm -rf "$d" 2>/dev/null && log "已删除: $d"
            done
            ;;
    esac
    log "卸载完成"
    exit 0
fi

# ── 帮助 ──
if [ "${HELP:-0}" = "1" ]; then
    echo ""
    banner "Bitmagnet-Indexer 安装脚本"
    echo ""
    echo "  安装:  bash install.sh"
    echo "         bash <(curl -sL ${RELEASE_BASE}/install.sh)"
    echo "         bash install.sh --mirror https://ghproxy.com   # 国内镜像加速"
    echo ""
    echo "  卸载:  bash install.sh --uninstall"
    exit 0
fi

# ── 1. 检查 Docker ──
command -v docker >/dev/null 2>&1 || err "Docker 未安装，请先安装 Docker"
docker info >/dev/null 2>&1 || err "Docker 服务未运行或无权限"
log "Docker 正常"

# ── 2. 架构检测 ──
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64|armv8*)  IMG_FILE="search-engine-arm64.tar.gz"  ; IMG_NAME="arm64" ;;
    x86_64|amd64)          IMG_FILE="search-engine-amd64.tar.gz" ; IMG_NAME="amd64" ;;
    *)                     err "不支持的架构: $ARCH (需要 aarch64/arm64 或 x86_64/amd64)" ;;
esac
log "检测到架构: $IMG_NAME"

# ── 3. 获取镜像（优先本地，否则下载） ──
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
LOCAL_IMG="$SCRIPT_DIR/$IMG_FILE"

if [ -f "$LOCAL_IMG" ]; then
    log "使用本地镜像: $LOCAL_IMG"
    IMG_PATH="$LOCAL_IMG"
else
    DOWNLOAD_URL="$RELEASE_URL/$IMG_FILE"
    log "下载镜像 ($IMG_NAME, ~65 MB)..."
    log "来源: $DOWNLOAD_URL"

    TMP_DIR=$(mktemp -d)
    IMG_PATH="$TMP_DIR/$IMG_FILE"

    if command -v wget >/dev/null 2>&1; then
        wget -q --show-progress "$DOWNLOAD_URL" -O "$IMG_PATH" || err "下载失败"
    elif command -v curl >/dev/null 2>&1; then
        curl -# -L "$DOWNLOAD_URL" -o "$IMG_PATH" || err "下载失败"
    else
        err "需要 wget 或 curl 来下载镜像，请先安装"
    fi
    log "下载完成 ($(du -h "$IMG_PATH" | cut -f1))"
fi

# ── 4. 配置参数（交互式） ──
echo ""
banner "数据库连接（bitmagnet 的 PostgreSQL）"
read -p "  PG 主机地址 [127.0.0.1]: " PG_HOST
PG_HOST="${PG_HOST:-127.0.0.1}"
read -p "  PG 端口 [5432]: " PG_PORT
PG_PORT="${PG_PORT:-5432}"
read -p "  PG 数据库名 [bitmagnet]: " PG_DB
PG_DB="${PG_DB:-bitmagnet}"
read -p "  PG 用户 [bitmagnet]: " PG_USER
PG_USER="${PG_USER:-bitmagnet}"
read -rsp "  PG 密码 [bitmagnet]: " PG_PASSWORD
echo ""
PG_PASSWORD="${PG_PASSWORD:-bitmagnet}"

echo ""
banner "qBittorrent（推送下载用，可选）"
read -p "  qB 主机地址 [留空跳过]: " QB_HOST
if [ -n "$QB_HOST" ]; then
    read -p "  qB 端口 [8080]: " QB_PORT
    QB_PORT="${QB_PORT:-8080}"
    read -p "  qB 用户 [admin]: " QB_USER
    QB_USER="${QB_USER:-admin}"
    read -rsp "  qB 密码 [adminadmin]: " QB_PASSWORD
    echo ""
    QB_PASSWORD="${QB_PASSWORD:-adminadmin}"
    read -p "  下载保存路径 [/mnt/Storage1/downloads]: " QB_SAVE
    QB_SAVE="${QB_SAVE:-/mnt/Storage1/downloads}"
    read -p "  分类标签 [radar]: " QB_CATEGORY
    QB_CATEGORY="${QB_CATEGORY:-radar}"
fi

echo ""
banner "数据存储"
read -p "  Docker 数据卷映射宿主路径 [$DATA_DIR_DEFAULT]: " DATA_DIR
DATA_DIR="${DATA_DIR:-$DATA_DIR_DEFAULT}"
read -p "  面板端口 [3001]: " WEB_PORT
WEB_PORT="${WEB_PORT:-3001}"

# ── 5. 确认 ──
echo ""
echo -e "${CYAN}══════════════════════════════════════${NC}"
echo -e "  架构: ${BOLD}$IMG_NAME${NC}"
echo -e "  镜像: $IMG_FILE"
echo -e "  数据: ${BOLD}$DATA_DIR${NC}"
echo -e "  PG:   ${BOLD}$PG_HOST:$PG_PORT/$PG_DB${NC} ($PG_USER)"
[ -n "$QB_HOST" ] && echo -e "  qB:   ${BOLD}$QB_HOST:$QB_PORT${NC} ($QB_USER)" || echo -e "  qB:   ${YELLOW}未配置${NC}"
echo -e "  面板: http://${PG_HOST}:${WEB_PORT}"
echo -e "${CYAN}══════════════════════════════════════${NC}"
echo ""
read -p "确认安装？[Y/n]: " CONFIRM
case "$CONFIRM" in
    [Nn]*) echo "已取消"; exit 0 ;;
esac

# ── 6. 导入镜像 ──
echo ""
log "导入 Docker 镜像..."
docker load < "$IMG_PATH" | tail -1

# ── 7. 建数据目录 ──
log "创建数据目录: $DATA_DIR"
mkdir -p "$DATA_DIR"

# ── 8. 停止旧容器 ──
log "停止旧容器（如果存在）..."
docker rm -f "$CONTAINER" 2>/dev/null || true

# ── 9. 启容器 ──
echo ""
log "启动容器..."

CMD="docker run -d --name $CONTAINER --network host --restart unless-stopped"
CMD="$CMD -v $DATA_DIR:/data"
CMD="$CMD -e PG_HOST=$PG_HOST -e PG_PORT=$PG_PORT -e PG_DB=$PG_DB -e PG_USER=$PG_USER -e PG_PASSWORD=$PG_PASSWORD"
if [ -n "$QB_HOST" ]; then
    CMD="$CMD -e QB_HOST=$QB_HOST -e QB_PORT=$QB_PORT -e QB_USER=$QB_USER -e QB_PASSWORD=$QB_PASSWORD"
    CMD="$CMD -e QB_SAVE_PATH=$QB_SAVE -e QB_CATEGORY=$QB_CATEGORY"
fi
CMD="$CMD -e API_PORT=$WEB_PORT search-engine:latest"

eval "$CMD"

sleep 3
if docker ps --filter "name=$CONTAINER" --format "{{.Status}}" | grep -q Up; then
    echo ""
    banner "🎉 安装成功！"
    echo -e "  访问:  ${BOLD}http://${PG_HOST}:${WEB_PORT}${NC}"
    echo -e "  日志:  ${CYAN}docker logs -f $CONTAINER${NC}"
    echo ""
    log "首次启动会自动创建 subs schema（无需手动建表）"
    log "在面板「系统设置 → 订阅管理」添加关键词即可开始使用"
    echo ""
    log "卸载:  bash $0 --uninstall"
else
    err "容器启动失败，检查日志: docker logs $CONTAINER"
fi

# ── 清理临时文件 ──
[ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR" 2>/dev/null || true
