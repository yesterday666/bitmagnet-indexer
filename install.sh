#!/bin/bash
# ==========================================================================
#  Bitmagnet-Indexer · 一键安装 / 卸载 / 更新脚本
#  自动检测最新版本，从 GitHub Release 下载对应架构的 Docker 镜像
#  用法:
#    安装: bash <(curl -sL https://github.com/yesterday666/bitmagnet-indexer/releases/latest/download/install.sh)
#         bash install.sh --mirror https://ghproxy.com   # 国内镜像加速
#    更新: bash install.sh --update
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
        --update|--upgrade) UPDATE=1; shift ;;
        --uninstall|-u) UNINSTALL=1; shift ;;
        --help|-h) HELP=1; shift ;;
        *) warn "忽略未知参数: $1"; shift ;;
    esac
done

# ── 获取最新版本 ──
log "获取最新版本信息..."
GITHUB_API="https://api.github.com/repos/yesterday666/bitmagnet-indexer/releases/latest"
if [ -n "$MIRROR" ]; then
    MIRROR="${MIRROR%/}"
    GITHUB_API="${MIRROR}/https://api.github.com/repos/yesterday666/bitmagnet-indexer/releases/latest"
fi

# 用 curl 或 wget 获取最新 release tag
LATEST_JSON=$(curl -sL "$GITHUB_API" 2>/dev/null || wget -qO- "$GITHUB_API" 2>/dev/null)
LATEST_TAG=$(echo "$LATEST_JSON" | python3 -c "
import sys,json; d=json.load(sys.stdin)
if 'tag_name' in d:
    print(d['tag_name'])
else:
    print('v1.0.0')
" 2>/dev/null || echo "v1.0.0")

RELEASE_BASE="https://github.com/yesterday666/bitmagnet-indexer/releases/download/$LATEST_TAG"
if [ -n "$MIRROR" ]; then
    RELEASE_URL="${MIRROR}/${RELEASE_BASE#https://}"
else
    RELEASE_URL="$RELEASE_BASE"
fi
log "最新版本: $LATEST_TAG"

# ── 帮助 ──
if [ "${HELP:-0}" = "1" ]; then
    echo ""
    banner "Bitmagnet-Indexer 使用说明"
    echo ""
    echo "  安装:"
    echo "    bash <(curl -sL ${RELEASE_BASE}/install.sh)"
    echo "    bash install.sh --mirror https://ghproxy.com"
    echo ""
    echo "  更新（保留配置和数据）:"
    echo "    bash install.sh --update"
    echo "    bash install.sh --mirror https://ghproxy.com --update"
    echo ""
    echo "  卸载:"
    echo "    bash install.sh --uninstall"
    echo ""
    echo "  更多信息: https://github.com/yesterday666/bitmagnet-indexer"
    exit 0
fi

# ── 卸载模式 ──
if [ "${UNINSTALL:-0}" = "1" ]; then
    echo ""
    banner "🗑️  卸载 Bitmagnet-Indexer"
    echo "即将停止并删除容器，数据目录可选保留。"
    echo ""
    read -p "确认卸载？[y/N]: " CONFIRM
    case "$CONFIRM" in
        [Yy]*|"yes"|"YES") ;;
        *) echo "已取消"; exit 0 ;;
    esac
    echo ""
    docker rm -f "$CONTAINER" 2>/dev/null && log "容器已删除" || warn "容器不存在"
    echo ""
    read -p "是否删除数据目录（SQLite 数据库、日志等）？ [y/N]: " DEL_DATA
    case "$DEL_DATA" in
        [Yy]*|"yes"|"YES")
            for d in "$DATA_DIR_DEFAULT" "/DATA/AppData/search_engine_data" "/data/search_engine_data"; do
                [ -d "$d" ] && rm -rf "$d" 2>/dev/null && log "已删除: $d"
            done
            ;;
    esac
    banner "卸载完成 ✅"
    exit 0
fi

# ── 更新模式 ──
if [ "${UPDATE:-0}" = "1" ]; then
    echo ""
    banner "🔄 更新 Bitmagnet-Indexer 到 $LATEST_TAG"

    # 检查旧容器是否存在
    OLD_IMAGE=$(docker inspect "$CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || echo "")
    if [ -z "$OLD_IMAGE" ]; then
        warn "未找到运行中的容器，将执行全新安装"
    else
        log "检测到旧版本: $OLD_IMAGE"
    fi

    # 备份旧容器配置
    OLD_ENV=$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{.}}{{"\n"}}{{end}}' 2>/dev/null || true)
    OLD_DATA_DIR=$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' 2>/dev/null | head -1)

    # 下载新镜像
    DOWNLOAD_URL="$RELEASE_URL/search-engine-$(uname -m | sed 's/aarch64/arm64/;s/x86_64/amd64/').tar.gz"
    log "下载新镜像... ($LATEST_TAG)"
    TMP_DIR=$(mktemp -d)
    IMG_PATH="$TMP_DIR/$(basename "$DOWNLOAD_URL")"
    if command -v wget >/dev/null 2>&1; then
        wget -q --show-progress "$DOWNLOAD_URL" -O "$IMG_PATH" || err "下载失败"
    else
        curl -# -L "$DOWNLOAD_URL" -o "$IMG_PATH" || err "下载失败"
    fi

    # 导入新镜像
    docker load < "$IMG_PATH" | tail -1

    # 停止旧容器
    log "停止旧容器..."
    docker rm -f "$CONTAINER" 2>/dev/null || true

    # 从旧环境变量重建（或提示手动配置）
    DATA_DIR="${OLD_DATA_DIR:-$DATA_DIR_DEFAULT}"
    PG_HOST=$(echo "$OLD_ENV" | grep PG_HOST | cut -d= -f2 || echo "")
    PG_PORT=$(echo "$OLD_ENV" | grep PG_PORT | cut -d= -f2 || echo "5432")
    PG_DB=$(echo "$OLD_ENV" | grep PG_DB | cut -d= -f2 || echo "bitmagnet")
    PG_USER=$(echo "$OLD_ENV" | grep PG_USER | cut -d= -f2 || echo "bitmagnet")
    PG_PASSWORD=$(echo "$OLD_ENV" | grep PG_PASSWORD | cut -d= -f2 || echo "bitmagnet")
    QB_HOST=$(echo "$OLD_ENV" | grep QB_HOST | cut -d= -f2 || echo "")
    QB_PORT=$(echo "$OLD_ENV" | grep QB_PORT | cut -d= -f2 || echo "8080")
    QB_USER=$(echo "$OLD_ENV" | grep QB_USER | cut -d= -f2 || echo "admin")
    QB_PASSWORD=$(echo "$OLD_ENV" | grep QB_PASSWORD | cut -d= -f2 || echo "adminadmin")
    QB_SAVE=$(echo "$OLD_ENV" | grep QB_SAVE_PATH | cut -d= -f2 || echo "/mnt/Storage1/downloads")
    QB_CATEGORY=$(echo "$OLD_ENV" | grep QB_CATEGORY | cut -d= -f2 || echo "radar")
    WEB_PORT=$(echo "$OLD_ENV" | grep API_PORT | cut -d= -f2 || echo "3001")

    log "已保留旧配置，启动新版本..."
else
    # ── 安装模式 ──
    echo ""
    banner "📦 安装 Bitmagnet-Indexer $LATEST_TAG"

    # 检查 Docker
    command -v docker >/dev/null 2>&1 || err "Docker 未安装"
    log "Docker 正常"

    # 架构检测
    ARCH=$(uname -m)
    case "$ARCH" in
        aarch64|arm64|armv8*)  IMG_FILE="search-engine-arm64.tar.gz" ;;
        x86_64|amd64)          IMG_FILE="search-engine-amd64.tar.gz" ;;
        *) err "不支持的架构: $ARCH (需要 aarch64/arm64 或 x86_64/amd64)" ;;
    esac

    # 获取镜像（优先本地，否则下载）
    SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
    if [ -f "$SCRIPT_DIR/$IMG_FILE" ]; then
        log "使用本地镜像: $IMG_FILE"
        IMG_PATH="$SCRIPT_DIR/$IMG_FILE"
    else
        DOWNLOAD_URL="$RELEASE_URL/$IMG_FILE"
        log "下载镜像 (~65 MB)..."
        TMP_DIR=$(mktemp -d)
        IMG_PATH="$TMP_DIR/$IMG_FILE"
        if command -v wget >/dev/null 2>&1; then
            wget -q --show-progress "$DOWNLOAD_URL" -O "$IMG_PATH" || err "下载失败"
        else
            curl -# -L "$DOWNLOAD_URL" -o "$IMG_PATH" || err "下载失败"
        fi
    fi

    # 交互式配置
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
    echo ""; PG_PASSWORD="${PG_PASSWORD:-bitmagnet}"

    echo ""
    banner "qBittorrent（可选）"
    read -p "  qB 主机地址 [留空跳过]: " QB_HOST
    if [ -n "$QB_HOST" ]; then
        read -p "  qB 端口 [8080]: " QB_PORT;       QB_PORT="${QB_PORT:-8080}"
        read -p "  qB 用户 [admin]: " QB_USER;       QB_USER="${QB_USER:-admin}"
        read -rsp "  qB 密码 [adminadmin]: " QB_PASSWORD; echo ""; QB_PASSWORD="${QB_PASSWORD:-adminadmin}"
        read -p "  下载保存路径 [/mnt/Storage1/downloads]: " QB_SAVE;    QB_SAVE="${QB_SAVE:-/mnt/Storage1/downloads}"
        read -p "  分类标签 [radar]: " QB_CATEGORY;  QB_CATEGORY="${QB_CATEGORY:-radar}"
    fi

    echo ""
    banner "数据存储"
    read -p "  Docker 数据卷路径 [$DATA_DIR_DEFAULT]: " DATA_DIR
    DATA_DIR="${DATA_DIR:-$DATA_DIR_DEFAULT}"
    read -p "  面板端口 [3001]: " WEB_PORT
    WEB_PORT="${WEB_PORT:-3001}"

    # 确认
    echo ""
    echo -e "${CYAN}══════════════════════════════════════${NC}"
    echo -e "  版本: ${BOLD}$LATEST_TAG${NC}"
    echo -e "  数据: ${BOLD}$DATA_DIR${NC}"
    echo -e "  PG:   ${BOLD}$PG_HOST:$PG_PORT/$PG_DB${NC}"
    [ -n "$QB_HOST" ] && echo -e "  qB:   ${BOLD}$QB_HOST:$QB_PORT${NC}" || echo -e "  qB:   ${YELLOW}未配置${NC}"
    echo -e "  面板: http://$PG_HOST:$WEB_PORT"
    echo -e "${CYAN}══════════════════════════════════════${NC}"
    read -p "确认安装？[Y/n]: " CONFIRM
    case "$CONFIRM" in [Nn]*) exit 0;; esac
fi

# ── 导入镜像 ──
echo ""
log "导入 Docker 镜像..."
docker load < "$IMG_PATH" | tail -1

# ── 建数据目录 ──
mkdir -p "$DATA_DIR"

# ── 停止旧容器 ──
docker rm -f "$CONTAINER" 2>/dev/null || true

# ── 启动新容器 ──
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
    if [ "${UPDATE:-0}" = "1" ]; then
        banner "🎉 更新成功！$LATEST_TAG"
        echo -e "  面板: ${BOLD}http://$PG_HOST:$WEB_PORT${NC}"
    else
        banner "🎉 安装成功！$LATEST_TAG"
        echo -e "  访问:  ${BOLD}http://$PG_HOST:$WEB_PORT${NC}"
        echo -e "  日志:  ${CYAN}docker logs -f $CONTAINER${NC}"
        echo ""
        log "首次启动会自动创建 subs schema（无需手动建表）"
        log "在面板「系统设置 → 订阅管理」添加关键词即可开始使用"
        echo ""
        log "更新:  bash $0 --update"
        log "卸载:  bash $0 --uninstall"
    fi
else
    err "容器启动失败，检查日志: docker logs $CONTAINER"
fi

# ── 清理临时文件 ──
rm -rf "${TMP_DIR:-/tmp/nonexistent}" 2>/dev/null || true
