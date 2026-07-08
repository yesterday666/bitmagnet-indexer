"""
REST API 服务器 (v2)
=====================
提供所有功能的 HTTP 接口：

搜索（支持分页）：
  POST /api/search        — 提交搜索（优先缓存，否则后台异步）
  GET  /api/search/<id>   — 查询任务状态/结果（支持 ?page=&page_size=）
  POST /api/search/batch  — 批量提交搜索

雷达：
  GET  /api/radar/status            — 雷达状态

订阅 + 挖掘（合并入口）：
  GET  /api/subscriptions           — 订阅列表（含挖掘状态）
  POST /api/subscriptions           — 添加订阅（自动启动挖掘）
  DELETE /api/subscriptions/<id>    — 删除订阅（同时停止挖掘）

挖掘机：
  POST /api/miner/start     — 单独启动挖掘
  POST /api/miner/stop      — 停止挖掘
  GET  /api/miner/status    — 挖掘状态

清理：
  GET  /api/cleanup/status   — 清理状态
  POST /api/cleanup/run      — 手动触发清理

系统：
  GET  /api/health           — 健康检查
  GET  /api/stats            — 全局统计
"""

from flask import Flask, request, jsonify
import signal
import sys
import time

import config
from database import init_sqlite, _get_sqlite_conn, pg_cursor
import requests as _requests
import urllib.parse
import re

# ⚠️ 必须在导入其他模块前初始化 SQLite 表，因为模块级单例在构造时会读写 SQLite
init_sqlite()
from auto_cleanup import cleaner
from qb_client import qb
from logger_setup import log
from subs_store import subs_store   # 精选小库（双库架构，取代旧 search_cache/radar/miner）


# ══════════════════════════════════════════════════════════════
# 多源预览/封面解析（番号封面 → whatslink，谁出图用谁）
# ══════════════════════════════════════════════════════════════
def _preview_proxies():
    return {"http": config.IMAGE_PROXY, "https": config.IMAGE_PROXY} if config.IMAGE_PROXY else None


_WL_STATE = {"down_until": 0}   # whatslink 健康冷却（挖了就一段时间不再试）

# 番号封面 CDN 模板（按顺序试，取到第一张真图即停）— {id}=DMM content_id 如 mide00083
PREVIEW_CDN_TEMPLATES = getattr(config, "PREVIEW_CDN_TEMPLATES", None) or [
    "https://pics.dmm.co.jp/digital/video/{id}/{id}pl.jpg",              # DMM 数字版 大图
    "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/{id}/{id}pl.jpg", # DMM AWS 镜像
    "https://pics.dmm.co.jp/mono/movie/adult/{id}/{id}pl.jpg",          # DMM 实体版(旧片)
    "https://pics.dmm.co.jp/digital/video/{id}/{id}ps.jpg",             # 小图兑底
]


def _jav_codes(name: str):
    """从名称提取番号 → (DMM content_id, 显示码)。如 MIDE-083 → (mide00083, MIDE-083)。"""
    out, seen = [], set()
    for m in re.finditer(r'([A-Za-z]{2,6})[-_ ]?(\d{2,6})', name or ""):
        label, num = m.group(1).lower(), m.group(2)
        try:
            cid = f"{label}{int(num):05d}"
        except ValueError:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, f"{label.upper()}-{num}"))
        if len(out) >= 3:
            break
    return out


def _is_real_image(url: str) -> bool:
    """经代理拉图，判断是否真图（排除 DMM 占位图/无图，它们通常 < 8KB）。"""
    try:
        r = _requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.dmm.co.jp/",
                     "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
            proxies=_preview_proxies(), timeout=6, stream=True,
        )
        if r.status_code != 200:
            r.close(); return False
        if "image" not in r.headers.get("Content-Type", ""):
            r.close(); return False
        cl = int(r.headers.get("Content-Length") or 0)
        r.close()
        return not (cl and cl < 8000)
    except Exception:
        return False


def resolve_previews(hash_hex: str, name: str) -> list:
    """多源预览解析：番号封面(服务端校验只留真图) → whatslink(复活自动生效)。"""
    out = []
    for cid, disp in _jav_codes(name):
        if len(out) >= 6:
            break
        for tpl in PREVIEW_CDN_TEMPLATES:
            url = tpl.format(id=cid)
            if _is_real_image(url):
                out.append({"url": f"/api/proxy/image?url={urllib.parse.quote(url)}", "label": f"封面 {disp}"})
                break   # 该番号取到一张真图即可，试下一个番号
    if len(out) < 4 and config.WHATSLINK_ENABLED and time.time() >= _WL_STATE["down_until"]:
        try:
            wl = f"{config.WHATSLINK_API}?url={urllib.parse.quote('magnet:?xt=urn:btih:'+hash_hex)}"
            r = _requests.get(wl, proxies=_preview_proxies(), timeout=6)
            if r.status_code == 200:
                _WL_STATE["down_until"] = 0
                d = r.json()
                if isinstance(d, dict):
                    if d.get("poster"):
                        out.append({"url": f"/api/proxy/screenshot?url={urllib.parse.quote(d['poster'])}", "label": "预览封面"})
                    for i, img in enumerate(d.get("screenshots", [])[:8]):
                        iu = img.get("screenshot") if isinstance(img, dict) else (img if isinstance(img, str) else "")
                        if iu:
                            out.append({"url": f"/api/proxy/screenshot?url={urllib.parse.quote(iu)}", "label": f"截图#{i+1}"})
        except Exception as e:
            # whatslink 不可达 → 5 分钟内不再重试，避免每次详情页白等 6s
            _WL_STATE["down_until"] = time.time() + 300
            log.info("whatslink 不可用，5分钟内跳过: %s", e)
    return out


# ── 轻量设置持久化（SQLite）：让图片代理等设置跨重启不丢 ──
def _save_setting(key, value):
    conn = _get_sqlite_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()

def _load_settings():
    conn = _get_sqlite_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    rows = conn.execute("SELECT key,value FROM app_settings").fetchall()
    kv = {r["key"]: r["value"] for r in rows}
    if kv.get("image_proxy") is not None:
        config.IMAGE_PROXY = kv["image_proxy"]
        log.info("已加载持久化图片代理: %s", config.IMAGE_PROXY or "(空)")
    # qBittorrent 设置
    if kv.get("qb_host"):      config.QB_HOST = kv["qb_host"]
    if kv.get("qb_port"):
        try: config.QB_PORT = int(kv["qb_port"])
        except (TypeError, ValueError): pass
    if kv.get("qb_user"):      config.QB_USER = kv["qb_user"]
    if kv.get("qb_password") is not None: config.QB_PASSWORD = kv["qb_password"]
    if kv.get("qb_category"):  config.QB_CATEGORY = kv["qb_category"]
    if kv.get("qb_save_path") is not None: config.QB_SAVE_PATH = kv["qb_save_path"]
    if any(k.startswith("qb_") for k in kv):
        qb.reconfigure()
        log.info("已加载持久化 qBittorrent 设置: %s:%s", config.QB_HOST, config.QB_PORT)

# ══════════════════════════════════════════════════════════════════
# Flask 应用
# ══════════════════════════════════════════════════════════════════

app = Flask(__name__)

# ── CORS 支持（允许控制台页面跨端口访问 API）─────────
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/<path:dummy>", methods=["OPTIONS"])
@app.route("/api", methods=["OPTIONS"])
def handle_options(dummy=None):
    return "", 204


# ── 健康检查 ────────────────────────────────────────────────────

@app.route("/")
@app.route("/panel")
def serve_panel():
    """提供 Web 控制台页面。"""
    import os
    panel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
    if os.path.exists(panel_path):
        with open(panel_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
    return "<h1>Panel not found</h1>", 404


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "radar": subs_store._running,
        "cleaner": cleaner._running,
    })


# ══════════════════════════════════════════════════════════════════
# 搜索 API（支持分页）
# ══════════════════════════════════════════════════════════════════

@app.route("/api/search", methods=["POST"])
def api_search():
    """
    提交搜索。请求体 JSON:
      {"keyword": "...", "filter_time": "", "filter_size": "", "sort_type": "date",
       "page": 1, "page_size": 50}
    """
    data = request.get_json(force=True, silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if len(keyword) < 2:
        return jsonify({"error": "关键词至少 2 个字符"}), 400

    page = max(1, int(data.get("page", 1)))
    page_size = min(max(1, int(data.get("page_size", 50))), 200)
    sort_type = data.get("sort_type", "date")
    # mode: smart(智能番号) / exact(精确) / fuzzy(模糊)；兼容旧 exact 布尔
    mode = data.get("mode") or ("exact" if data.get("exact") else "fuzzy")

    # 【双库】搜索只查精选小库（全量 GIN，毫秒级）
    result = subs_store.search(keyword, page=page, page_size=page_size, sort_type=sort_type, mode=mode, filter_time=data.get("filter_time", ""), filter_size=data.get("filter_size", ""))
    # 搜索即自动订阅：没订阅过的新词 → 后台全量回溯 + 增量监控（不推 qB），结果边灌边出
    result["subscription"] = subs_store.ensure_subscription(keyword)
    return jsonify(result)


# 旧 /api/search/batch 与 /api/search/<task_id>（基于 search_cache 后台全表扫描）已废弃删除。
# 新搜索直接查小库、即时返回，无需任务轮询。


# ══════════════════════════════════════════════════════════════════
# 订阅 API（合并挖掘 — 添加订阅时自动启动挖掘）
# ══════════════════════════════════════════════════════════════════

@app.route("/api/subscriptions", methods=["GET"])
def api_subscriptions_list():
    # 【双库】订阅改从精选小库状态机读取；保留 miner 子对象以兼容前端。
    subs = subs_store.list_subscriptions()
    result = []
    for s in subs:
        result.append({
            "id": s["id"], "keyword": s["keyword"],
            "enabled": 1 if s["enabled"] else 0,
            "created_at": s["created_at"],
            "status": s["status"],
            "push_qb": s["push_qb"],
            "matched_count": s["matched_count"],
            "scanned_count": s["scanned_count"],
            "last_error": s["last_error"],
            # 前端兼容：沿用旧 miner 字段形状
            "miner": {
                "status": s["status"],
                "scanned_count": s["scanned_count"],
                "match_count": s["matched_count"],
                "estimated_total": 0,
                "running": s["running"],
            },
        })
    return jsonify({"subscriptions": result, "count": len(result)})


@app.route("/api/subscriptions", methods=["POST"])
def api_subscriptions_add():
    """
    添加订阅关键词（同时自动启动旧数据挖掘）。
    请求体 JSON:
      {"keyword": "1080p"}  或  {"keywords": ["1080p", "4K", "电影"]}
    """
    data = request.get_json(force=True, silent=True) or {}
    keywords = data.get("keywords")
    if not keywords:
        kw = data.get("keyword", "").strip()
        if kw:
            keywords = [kw]
    if not keywords:
        return jsonify({"error": "keyword 或 keywords 不能为空"}), 400

    # 【双库】前端“订阅”按钮 = 指定内容(push_qb=True)：回溯灌小库 + 监控新增推 qB
    added = []
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        r = subs_store.add_subscription(kw, push_qb=True)
        if r.get("success"):
            added.append(kw)

    log.info("添加指定内容订阅: %s", added)
    return jsonify({
        "success": True,
        "added": added,
        "count": len(added),
        "miner_started": [],   # 兼容字段（旧前端可能读取）
    })


@app.route("/api/subscriptions/<int:sub_id>", methods=["DELETE"])
def api_subscriptions_delete(sub_id):
    # 【双库】删除订阅及其映射（默认保留已入小库的种子）
    subs_store.delete_subscription(sub_id)
    return jsonify({"success": True})


@app.route("/api/subscriptions/<int:sub_id>/toggle", methods=["POST"])
def api_subscriptions_toggle(sub_id):
    subs = {s["id"]: s for s in subs_store.list_subscriptions()}
    cur = subs.get(sub_id)
    new_enabled = not (cur["enabled"] if cur else True)
    subs_store.set_enabled(sub_id, new_enabled)
    return jsonify({"success": True, "subscription": {"id": sub_id, "enabled": 1 if new_enabled else 0}})


@app.route("/api/subscriptions/<int:sub_id>/push_qb", methods=["POST"])
def api_subscriptions_push_qb(sub_id):
    """将订阅标记为/取消「指定内容」（监控新增是否推 qB）。"""
    data = request.get_json(force=True, silent=True) or {}
    push = bool(data.get("push_qb", True))
    return jsonify(subs_store.set_push_qb(sub_id, push))


# ══════════════════════════════════════════════════════════════════
# 雷达 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/radar/status")
def api_radar_status():
    # 【双库】雷达 = 小库增量监控状态
    return jsonify(subs_store.monitor_status())


# ══════════════════════════════════════════════════════════════════
# 挖掘机 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/miner/start", methods=["POST"])
def api_miner_start():
    data = request.get_json(force=True, silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "keyword 不能为空"}), 400
    return jsonify(subs_store.start_by_keyword(keyword))


@app.route("/api/miner/stop", methods=["POST"])
def api_miner_stop():
    data = request.get_json(force=True, silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "keyword 不能为空"}), 400
    return jsonify(subs_store.pause_by_keyword(keyword))


@app.route("/api/miner/status")
def api_miner_status_all():
    return jsonify(subs_store.miner_status())


@app.route("/api/miner/status/<keyword>")
def api_miner_status_one(keyword):
    return jsonify(subs_store.miner_status(keyword))


@app.route("/api/miner/delete/<keyword>", methods=["DELETE"])
def api_miner_delete(keyword):
    """删除订阅任务（不动已入小库的种子）。"""
    result = subs_store.delete_by_keyword(keyword)
    log.info("订阅任务已删除: '%s'", keyword)
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# 种子详情 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/torrent/<hash_hex>/files")
def api_torrent_files(hash_hex):
    """获取种子文件列表。"""
    try:
        with pg_cursor() as cur:
            cur.execute("""
                SELECT index, path, extension, size
                FROM torrent_files
                WHERE info_hash = decode(%s, 'hex')
                ORDER BY index
                LIMIT 500
            """, (hash_hex,))
            rows = cur.fetchall()

        if not rows:
            return jsonify({"files": [], "message": "无文件列表"})

        files = []
        images = []
        img_exts = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico', 'tiff'}
        for r in rows:
            ext = (r[2] or "").lower()
            f = {
                "index": r[0],
                "path": r[1] or "",
                "extension": ext,
                "size": r[3] or 0,
                "is_image": ext in img_exts,
            }
            files.append(f)
            if f["is_image"]:
                images.append(f)

        return jsonify({
            "files": files,
            "images": images,
            "total_files": len(files),
        })
    except Exception as e:
        log.error("获取文件列表失败 %s: %s", hash_hex, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/torrent/<hash_hex>/detail")
def api_torrent_detail(hash_hex):
    """获取种子基本详情。"""
    try:
        with pg_cursor() as cur:
            cur.execute("""
                SELECT encode(t.info_hash, 'hex'), t.name, t.size,
                       COALESCE(t.files_count,0),
                       EXTRACT(EPOCH FROM t.created_at)::bigint,
                       COALESCE(tc.seeders, 0), COALESCE(tc.leechers, 0),
                       COALESCE(tc.content_type, ''), COALESCE(tc.video_resolution, ''),
                       COALESCE(tc.video_codec, ''), COALESCE(tc.release_group, '')
                FROM torrents t
                LEFT JOIN torrent_contents tc ON t.info_hash = tc.info_hash
                WHERE t.info_hash = decode(%s, 'hex')
                LIMIT 1
            """, (hash_hex,))
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "种子不存在"}), 404

        # 封面/预览改由 /api/torrent/<hash>/screenshots 多源解析(服务端校验)异步提供
        screenshots = []

        return jsonify({
            "hash": row[0][:40] if row[0] else hash_hex,
            "name": row[1],
            "size": int(row[2]) if row[2] else 0,
            "files_count": row[3],
            "created_at": int(row[4]) if row[4] else 0,
            "seeders": row[5] or 0,
            "leechers": row[6] or 0,
            "content_type": row[7] or "",
            "video_resolution": row[8] or "",
            "video_codec": row[9] or "",
            "release_group": row[10] or "",
            "magnet_uri": "magnet:?xt=urn:btih:" + hash_hex + "&dn=" + (row[1] or ""),
            "screenshots": screenshots,
        })
    except Exception as e:
        log.error("获取种子详情失败 %s: %s", hash_hex, e)
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# whatslink.info 截图代理（仿 Bitmagnet-Next-Web）
# ══════════════════════════════════════════════════════════════════

@app.route("/api/torrent/<hash_hex>/screenshots")
def api_torrent_screenshots(hash_hex):
    """多源预览：番号封面(DMM,服务端校验真图) → whatslink(复活自动生效)。谁出图用谁。"""
    try:
        name = ""
        with pg_cursor() as cur:
            cur.execute("SELECT name FROM torrents WHERE info_hash = decode(%s,'hex') LIMIT 1", (hash_hex,))
            r = cur.fetchone()
            if r:
                name = r[0] or ""
        images = resolve_previews(hash_hex, name)
        return jsonify({"screenshots": images, "count": len(images)})
    except Exception as e:
        log.warning("预览解析失败 %s: %s", hash_hex[:12], e)
        return jsonify({"screenshots": [], "error": str(e)})


@app.route("/api/torrent/<hash_hex>/push", methods=["POST"])
def api_torrent_push(hash_hex):
    """手动推送当前种子到 qBittorrent 下载（用户显式操作，无视订阅）。"""
    try:
        name = hash_hex
        with pg_cursor() as cur:
            cur.execute("SELECT name FROM torrents WHERE info_hash = decode(%s,'hex') LIMIT 1", (hash_hex,))
            r = cur.fetchone()
            if r and r[0]:
                name = r[0]
        if not qb.health_check():
            return jsonify({"success": False, "message": "qBittorrent 未连接"})
        res = qb.add_magnet(hash_hex, name, category=config.QB_CATEGORY, save_path=config.QB_SAVE_PATH)
        ok = bool(res.get("success"))
        # 记录推送日志
        try:
            conn = _get_sqlite_conn()
            conn.execute("INSERT INTO push_log (info_hash,name,size,keyword,source,pushed_at,success) VALUES (?,?,?,?,?,?,?)",
                         (hash_hex, name, 0, "", "manual", time.time(), 1 if ok else 0))
            conn.commit()
        except Exception:
            pass
        return jsonify({"success": ok, "message": res.get("message", "")})
    except Exception as e:
        log.warning("手动推送失败 %s: %s", hash_hex[:12], e)
        return jsonify({"success": False, "message": str(e)[:200]})


@app.route("/api/proxy/screenshot")
def api_proxy_screenshot():
    """二级代理：服务端抓取截图图片返回二进制（仿 Bitmagnet-Next-Web /api/preview/[id]）。"""
    url = request.args.get("url", "")
    if not url:
        return "", 400
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        proxies = None
        if config.IMAGE_PROXY:
            proxies = {"http": config.IMAGE_PROXY, "https": config.IMAGE_PROXY}
        resp = _requests.get(url, headers=headers, proxies=proxies, timeout=15)
        ct = resp.headers.get("Content-Type", "image/jpeg")
        return resp.content, 200, {
            "Content-Type": ct,
            "Cache-Control": "public, max-age=86400",
        }
    except Exception as e:
        log.warning("截图代理失败: %s", e)
        return "", 502


# ══════════════════════════════════════════════════════════════════
# 图片代理（DMM 等外部封面，支持 HTTP/SOCKS5 出口）
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# 测试连接 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/qb/test", methods=["POST"])
def api_qb_test():
    """测试 qBittorrent 连接。"""
    data = request.get_json(force=True, silent=True) or {}
    # 如果传了新配置，临时应用
    if data.get("host"):
        qb.base = f"http://{data['host']}:{data.get('port', config.QB_PORT)}/api/v2"
    if data.get("user"):
        import requests as _r
        qb.session = _r.Session()
        qb._logged_in = False
    if data.get("password"):
        config.QB_PASSWORD = data["password"]
    
    start = time.time()
    ok = qb.health_check()
    elapsed = round((time.time() - start) * 1000)
    return jsonify({
        "connected": ok,
        "latency_ms": elapsed,
        "host": config.QB_HOST,
        "port": config.QB_PORT,
    })


# ══════════════════════════════════════════════════════════════════
# 图片代理（支持 HTTP/SOCKS5）
# ══════════════════════════════════════════════════════════════════

@app.route("/api/proxy/config", methods=["GET"])
def api_proxy_config_get():
    return jsonify({"proxy_url": config.IMAGE_PROXY or "", "enabled": bool(config.IMAGE_PROXY)})


@app.route("/api/proxy/config", methods=["POST"])
def api_proxy_config_set():
    data = request.get_json(force=True, silent=True) or {}
    config.IMAGE_PROXY = (data.get("proxy_url") or "").strip()
    _save_setting("image_proxy", config.IMAGE_PROXY)   # 持久化，重启不丢
    log.info("图片代理已更新: %s", config.IMAGE_PROXY or "(已清空)")
    return jsonify({"success": True, "proxy_url": config.IMAGE_PROXY})

@app.route("/api/proxy/image")
def api_proxy_image():
    """代理外部图片，支持 HTTP/SOCKS5 出口代理。"""
    url = request.args.get("url", "")
    if not url:
        return "", 400
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.dmm.co.jp/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        proxies = None
        if config.IMAGE_PROXY:
            proxies = {"http": config.IMAGE_PROXY, "https": config.IMAGE_PROXY}
        resp = _requests.get(url, headers=headers, proxies=proxies, timeout=10, allow_redirects=True)
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return resp.content, resp.status_code if resp.status_code == 200 else 502, {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        }
    except Exception as e:
        log.warning("图片代理失败: %s", e)
        return "", 502


# ══════════════════════════════════════════════════════════════════
# 清理 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/cleanup/status")
def api_cleanup_status():
    return jsonify(cleaner.get_status())


# ══════════════════════════════════════════════════════════════════
# 统计 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    conn = _get_sqlite_conn()
    push_count = conn.execute("SELECT COUNT(*) as c FROM push_log").fetchone()["c"]
    cleaned = conn.execute("SELECT COUNT(*) as c FROM cleanup_log").fetchone()["c"]
    c = subs_store.counts()
    return jsonify({
        "cache_entries": c["small_torrents"],   # 小库种子总数（取代旧缓存条数）
        "subscriptions": c["subscriptions"],
        "total_pushed": push_count,
        "total_cleaned": cleaned,
        "miner_jobs": c["subscriptions"],
        "miner_total_scanned": 0,
        "miner_total_matched": 0,
        "qb_connected": qb.health_check(),
    })


@app.route("/api/dbstats")
def api_dbstats():
    """首页右下角数据框：小库/大库 数量+大小 + 爬虫新增速率。
    大库用 reltuples 估算(瞬时, 不跑全表 count); 大小用元数据; 速率走 created_at 索引查近60s。"""
    out = {"big_count": None, "big_size": None, "small_count": None, "small_size": None, "crawl_per_min": None}
    try:
        with pg_cursor() as cur:
            cur.execute("SELECT reltuples::bigint FROM pg_class WHERE oid='public.torrents'::regclass")
            r = cur.fetchone(); out["big_count"] = int(r[0]) if r and r[0] and r[0] > 0 else None
            cur.execute("SELECT pg_database_size(current_database())")
            out["big_size"] = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM subs.torrents")
            out["small_count"] = int(cur.fetchone()[0])
            cur.execute("""SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0)
                           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                           WHERE n.nspname='subs' AND c.relkind='r'""")
            out["small_size"] = int(cur.fetchone()[0])
            try:
                cur.execute("SELECT count(*) FROM public.torrents WHERE created_at > now() - interval '60 seconds'")
                out["crawl_per_min"] = int(cur.fetchone()[0])
            except Exception:
                out["crawl_per_min"] = None
    except Exception as e:
        log.warning("dbstats 失败: %s", e)
    return jsonify(out)


# ══════════════════════════════════════════════════════════════
# 精选小库管理（清空 / 备份）— 供 WebUI 系统设置
# ══════════════════════════════════════════════════════════════

@app.route("/api/subs/clear", methods=["POST"])
def api_subs_clear():
    """清空精选小库（种子/映射/订阅）。不影响 bitmagnet 大库。"""
    return jsonify(subs_store.clear_all())


@app.route("/api/subs/backup", methods=["POST"])
def api_subs_backup():
    """备份精选小库到指定路径（pg_dump -n subs）。"""
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"success": False, "message": "请指定备份路径"}), 400
    return jsonify(subs_store.backup(path))


# ══════════════════════════════════════════════════════════════════
# qBittorrent 配置 API
# ══════════════════════════════════════════════════════════════════

@app.route("/api/qb/config", methods=["GET"])
def api_qb_config_get():
    return jsonify({
        "host": config.QB_HOST,
        "port": config.QB_PORT,
        "user": config.QB_USER,
        "category": config.QB_CATEGORY,
        "save_path": config.QB_SAVE_PATH,
        "connected": qb.health_check(),
    })


@app.route("/api/qb/config", methods=["POST"])
def api_qb_config_set():
    data = request.get_json(force=True, silent=True) or {}
    # 统一更新 config.QB_*，持久化，再重建连接
    if data.get("host"):
        config.QB_HOST = str(data["host"]).strip(); _save_setting("qb_host", config.QB_HOST)
    if data.get("port"):
        try:
            config.QB_PORT = int(data["port"]); _save_setting("qb_port", str(config.QB_PORT))
        except (TypeError, ValueError):
            pass
    if data.get("user"):
        config.QB_USER = str(data["user"]).strip(); _save_setting("qb_user", config.QB_USER)
    if data.get("password"):
        config.QB_PASSWORD = data["password"]; _save_setting("qb_password", config.QB_PASSWORD)
    if data.get("category"):
        config.QB_CATEGORY = str(data["category"]).strip(); _save_setting("qb_category", config.QB_CATEGORY)
    if "save_path" in data:
        config.QB_SAVE_PATH = (data.get("save_path") or "").strip(); _save_setting("qb_save_path", config.QB_SAVE_PATH)
    qb.reconfigure()
    connected = qb.health_check()
    if connected and not cleaner._running:
        cleaner.start()
    return jsonify({"success": True, "connected": connected})


# ══════════════════════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════════════════════

def run_server():
    """启动 API 服务和所有后台线程。"""
    # 初始化
    init_sqlite()
    _load_settings()   # 加载持久化设置（图片代理等）

    # 【双库】启动精选小库：回溯 worker + 增量监控 worker + 启动恢复
    subs_store.start()
    log.info("精选小库（双库）已启动：回溯 + 监控")

    # 启动清理（qB 做种管理，保留）
    if qb.health_check():
        cleaner.start()
        log.info("qBittorrent 已连接，自动清理已启动")
    else:
        log.warning("qBittorrent 未连接，自动清理和推送功能不可用")

    # 信号处理
    def shutdown(sig, frame):
        log.info("收到退出信号，正在关闭...")
        subs_store.stop()
        cleaner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 启动 Flask
    log.info("=" * 60)
    log.info("双引擎搜索系统 v2 已启动")
    log.info("控制台: http://%s:%d", config.API_HOST, config.API_PORT)
    log.info("=" * 60)

    app.run(host=config.API_HOST, port=config.API_PORT,
            debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    run_server()
