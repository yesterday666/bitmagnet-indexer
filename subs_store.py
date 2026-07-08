"""
subs_store.py — 精选小库访问层 + 订阅状态机 + 全量回溯 / 增量监控
====================================================================
定位：本模块是「双库架构」的应用层核心。
  · 大库 public.torrents（bitmagnet 爬虫库）—— 只读，永不写、不加索引。
  · 小库 subs.*（同 PG 实例，独立 schema，全量 GIN）—— 搜索只打这里，毫秒级。

职责：
  1) search()         用户搜索，只查小库。
  2) 订阅状态机        pending → syncing(全量回溯) → monitoring(增量) → paused/failed
  3) _run_backfill()  全量回溯：跨 schema INSERT...SELECT + ON CONFLICT 防重，一次入库。
  4) _monitor_loop()  单线程一次扫描大库新增，内存比对所有监控关键词（仿 radar，省资源）。
  5) 可选 qB 推送      config.SUBS_PUSH_QB=True 时命中即推 qBittorrent（保留旧下载能力）。

游标：大库无自增 id，统一用 (created_at, info_hash) 复合水位线。
"""

import time
import threading
import queue
import os
import subprocess
import re as _re

import psycopg2

import config
from database import pg_cursor
from logger_setup import log

# ── 可调参数（config 未定义时用默认值，无需强制改 config.py）────────────
BACKFILL_BATCH   = int(getattr(config, "SUBS_BACKFILL_BATCH", 5000))    # 回溯每批
MONITOR_BATCH    = int(getattr(config, "SUBS_MONITOR_BATCH", 3000))     # 监控每批
MONITOR_INTERVAL = int(getattr(config, "SUBS_MONITOR_INTERVAL_SEC", 10))# 监控轮询间隔
# qB 推送为「按订阅粒度」控制：仅 push_qb=true 的「指定内容」订阅，
# 且仅在「监控」阶段命中大库真·新增时才推送；全量回溯的历史数据一律不推。


class SubsStore:
    def __init__(self):
        self._running = False
        self._lock = threading.Lock()
        self._backfill_q: "queue.Queue[int]" = queue.Queue()  # 待回溯的订阅 id（串行，弱机友好）
        self._backfill_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._active_backfill: int | None = None
        self._scan_conn = None   # 回溯专用连接（无 statement_timeout）
        self._suspend = threading.Event()   # 置位时暂停回溯/监控（清空时用）
        # pg_dump 所在容器（PG 跑在 docker 里）
        self._pg_container = getattr(config, "SUBS_PG_CONTAINER", "bitmagnet-db")

    # ══════════════════════════════════════════════════════════════
    # 1) 搜索 —— 只打小库
    # ══════════════════════════════════════════════════════════════
    @staticmethod
    def _detect_code(kw: str):
        """判断输入是否像番号（字母2-6 + 数字）。是则返回 (label小写, 去前导零的数字)。"""
        m = _re.match(r'^\s*([A-Za-z]{2,6})[-_ ]?(\d{1,6})\s*$', kw or "")
        if not m:
            return None
        return m.group(1).lower(), (m.group(2).lstrip('0') or '0')

    def _code_pattern(self, kw: str):
        """识别番号格式 → (regex_body, display)。body 不带边界，由调用方自行加 PG/Python 边界。
        支持: SSIS-790, FC2-PPV-1234567, 1PONDO-010112(-001), Caribbean, HEYZO 等。"""
        kw = (kw or "").strip()
        # ── FC2-PPV 系列 ──
        m = _re.match(r'^fc2[-_ ]?(?:ppv)?[-_ ]?(\d{5,7})$', kw, _re.I)
        if m:
            num = m.group(1)
            return r'fc2[-_ ]?(?:ppv[-_ ]?)?0*' + num, f"FC2-PPV-{num}"
        # ── 日期型无码厂牌: 1PONDO / Caribbean / HEYZO / Tokyo-Hot / Muramura (可带序号) ──
        m = _re.match(r'^(1pondo|cari(?:b|bbean|bpr|prx)?|heyzo|tokyo[-_ ]?hot|muramura)[-_ ]?(\d{2,8})(?:[-_ ](\d{1,3}))?$', kw, _re.I)
        if m:
            label = m.group(1).lower().replace('-', '').replace('_', '').replace(' ', '')
            if label.startswith('carib'):
                label = 'carib'
            num, seq = m.group(2), m.group(3)
            body = _re.escape(label) + r'[-_ ]?0*' + num
            disp = {"1pondo": "1PONDO", "carib": "Caribbean", "heyzo": "HEYZO",
                    "tokyohot": "Tokyo-Hot", "muramura": "Muramura"}.get(label, label.upper()) + f"-{num}"
            if seq:
                body += r'[-_ ]?0*' + seq
                disp += f"-{seq}"
            return body, disp
        # ── 标准番号 字母2-6 + 数字1-6 ──
        m = _re.match(r'^([A-Za-z]{2,6})[-_ ]?(\d{1,6})$', kw)
        if m:
            label, num = m.group(1).lower(), (m.group(2).lstrip('0') or '0')
            return label + r'[-_ ]?0*' + num, f"{label.upper()}-{num}"
        return None

    def _precise_push_match(self, kw: str, name: str) -> bool:
        """一个种子名是否精确匹配订阅关键词（qB 推送前最后把关）。
        番号→番号格式匹配；纯ASCII非番号→整词边界；含CJK→包含即可（CJK无子串噪声）。"""
        if not name:
            return False
        cp = self._code_pattern(kw)
        if cp:
            return bool(_re.search(r'(?<![A-Za-z0-9])' + cp[0] + r'(?![0-9])', name, _re.I))
        kw = kw.strip()
        if _re.search(r'[^\x00-\x7f]', kw):   # 含非ASCII(中/日文等) → 包含匹配
            return kw.lower() in name.lower()
        return bool(_re.search(r'(?<![A-Za-z0-9])' + _re.escape(kw) + r'(?![A-Za-z0-9])', name, _re.I))

    def search(self, keyword: str, page: int = 1, page_size: int = 50,
               sort_type: str = "date", mode: str = "fuzzy",
               filter_time: str = "", filter_size: str = "") -> dict:
        order = {
            "size":  "size DESC NULLS LAST",
            "count": "files_count DESC NULLS LAST",
        }.get(sort_type, "created_at DESC NULLS LAST")
        offset = (page - 1) * page_size

        import re as _re
        detected = None
        if mode == "smart":
            cp = self._code_pattern(keyword)
            if cp:
                match_sql = "name ~* %s"
                param = r'\y' + cp[0] + r'(?![0-9])'
                detected = cp[1]
            elif _re.fullmatch(r'[A-Za-z]{2,}', keyword.strip()):
                # 纯字母词(如 ncy/ssis/pnme) -> 整词边界，避免 age【ncy】类子串噪声
                match_sql = "name ~* %s"
                param = r'\y' + _re.escape(keyword.strip()) + r'\y'
                detected = keyword.strip().upper()
            else:
                match_sql = "name ILIKE %s"
                param = f"%{keyword}%"
        elif mode == "exact":
            match_sql = "name ~* %s"
            param = r'\y' + _re.escape(keyword) + r'\y'
        else:
            match_sql = "name ILIKE %s"
            param = f"%{keyword}%"


        # --- filter_time / filter_size conditions ---
        extra_where = ""
        merged_params = (param,)
        if filter_time:
            days_map = {"gt-1day": "1 day", "gt-7day": "7 days", "gt-31day": "31 days", "gt-365day": "365 days"}
            if filter_time in days_map:
                extra_where += " AND created_at > NOW() - INTERVAL %s"
                merged_params = merged_params + (days_map[filter_time],)
        if filter_size:
            size_map = {
                "lt100mb": ("size < %s", [104857600]),
                "gt100mb-lt500mb": ("size >= %s AND size < %s", [104857600, 524288000]),
                "gt500mb-lt1gb": ("size >= %s AND size < %s", [524288000, 1073741824]),
                "gt1gb-lt5gb": ("size >= %s AND size < %s", [1073741824, 5368709120]),
                "gt5gb": ("size >= %s", [5368709120]),
            }
            if filter_size in size_map:
                sql_part, vals = size_map[filter_size]
                extra_where += f" AND {sql_part}"
                for v in vals:
                    merged_params = merged_params + (v,)

        with pg_cursor() as cur:
            cur.execute(f"SELECT count(*) FROM subs.torrents WHERE {match_sql}{extra_where}", merged_params)
            total = cur.fetchone()[0]
            cur.execute(f"""
                SELECT encode(info_hash,'hex'), name, size, COALESCE(files_count,0),
                       EXTRACT(EPOCH FROM created_at)::bigint
                FROM subs.torrents
                WHERE {match_sql}{extra_where}
                ORDER BY {order}
                LIMIT %s OFFSET %s
            """, merged_params + (page_size, offset))
            rows = cur.fetchall()

        results = []
        for r in rows:
            magnet = "magnet:?xt=urn:btih:" + r[0] + "&dn=" + (r[1] or "")
            if r[2] and int(r[2]) > 0:
                magnet += "&xl=" + str(r[2])
            results.append({
                "hash": r[0], "name": r[1], "size": int(r[2]) if r[2] else 0,
                "files_count": r[3], "created_at": int(r[4]) if r[4] else 0,
                "magnet_uri": magnet,
            })

        return {
            "keyword": keyword, "results": results, "total_count": total,
            "page": page, "page_size": page_size,
            "has_more": offset + len(results) < total,
            "cached": True,        # 兼容前端：小库即时返回，等价于「命中缓存」
            "mode": mode,
            "detected_code": detected,
            "source": "subs",
        }

    # ══════════════════════════════════════════════════════════════
    # 2) 订阅 CRUD + 状态机
    # ══════════════════════════════════════════════════════════════
    def list_subscriptions(self) -> list[dict]:

        with pg_cursor() as cur:
            cur.execute("""
                SELECT id, keyword, status, matched_count, scanned_count,
                       EXTRACT(EPOCH FROM created_at)::bigint, enabled, last_error, push_qb
                FROM subs.subscriptions ORDER BY id
            """)
            rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "keyword": r[1], "status": r[2],
                "matched_count": r[3], "scanned_count": r[4],
                "created_at": r[5], "enabled": bool(r[6]),
                "last_error": r[7], "push_qb": bool(r[8]),
                "running": (r[0] == self._active_backfill),
            })
        return out

    def add_subscription(self, keyword: str, push_qb: bool = False) -> dict:
        """新增/复用订阅。
        push_qb=False -> 搜索自动订阅（只灌小库供搜索，永不推 qB）。
        push_qb=True  -> 指定内容订阅（灌小库 + 监控到大库新增时推 qB）。
        对已存在订阅：push_qb=True 可将其「升级」为指定内容（true 优先，不会被降级）。
        """
        keyword = keyword.strip()
        if not keyword:
            return {"success": False, "message": "关键词为空"}
        with pg_cursor(autocommit=True) as cur:
            cur.execute("""
                INSERT INTO subs.subscriptions (keyword, status, push_qb)
                VALUES (%s, 'pending', %s)
                ON CONFLICT (keyword) DO UPDATE
                    SET updated_at = now(),
                        push_qb = subs.subscriptions.push_qb OR EXCLUDED.push_qb
                RETURNING id, status, push_qb
            """, (keyword, push_qb))
            sid, status, eff_push = cur.fetchone()
        # 若从未回溯完成，则(重新)入队回溯
        if status in ("pending", "failed", "paused"):
            self.enqueue_backfill(sid)
        log.info("订阅: '%s' (id=%d, status=%s, push_qb=%s) 已入回溯队列", keyword, sid, status, eff_push)
        return {"success": True, "id": sid, "keyword": keyword, "status": status, "push_qb": eff_push}

    def set_push_qb(self, sub_id: int, push_qb: bool) -> dict:
        """手动把订阅标记为/取消「指定内容」（是否推 qB）。"""
        with pg_cursor(autocommit=True) as cur:
            cur.execute("UPDATE subs.subscriptions SET push_qb=%s, updated_at=now() WHERE id=%s",
                        (push_qb, sub_id))
        return {"success": True, "id": sub_id, "push_qb": push_qb}

    def ensure_subscription(self, keyword: str) -> dict:
        """搜索即订阅：该词从未订阅则自动创建（push_qb=False，不推 qB）。
        已存在则仅返回当前状态，不重复写入。"""
        keyword = keyword.strip()
        if not keyword:
            return {"exists": False}

        with pg_cursor() as cur:
            cur.execute("SELECT id, status, push_qb FROM subs.subscriptions WHERE keyword=%s", (keyword,))
            row = cur.fetchone()
        if row:
            return {"id": row[0], "status": row[1], "push_qb": bool(row[2]), "auto_subscribed": False}
        res = self.add_subscription(keyword, push_qb=False)
        res["auto_subscribed"] = True
        return res

    def delete_subscription(self, sub_id: int, purge_torrents: bool = False) -> dict:
        """删除订阅。默认只删订阅与映射；purge_torrents=True 时清掉不再被任何订阅引用的种子。"""
        with pg_cursor(autocommit=True) as cur:
            cur.execute("DELETE FROM subs.subscriptions WHERE id=%s", (sub_id,))
            if purge_torrents:
                cur.execute("""
                    DELETE FROM subs.torrents t
                    WHERE NOT EXISTS (
                        SELECT 1 FROM subs.subscription_matches m WHERE m.info_hash = t.info_hash
                    )
                """)
        return {"success": True}

    def set_enabled(self, sub_id: int, enabled: bool) -> dict:
        with pg_cursor(autocommit=True) as cur:
            cur.execute("UPDATE subs.subscriptions SET enabled=%s, updated_at=now() WHERE id=%s",
                        (enabled, sub_id))
        return {"success": True}

    def _set_status(self, sid: int, status: str, err: str = None):
        with pg_cursor(autocommit=True) as cur:
            cur.execute(
                "UPDATE subs.subscriptions SET status=%s, last_error=%s, updated_at=now() WHERE id=%s",
                (status, err, sid))

    # ══════════════════════════════════════════════════════════════
    # 3) 全量回溯（syncing）—— 跨 schema 一次入库 + ON CONFLICT 防重
    # ══════════════════════════════════════════════════════════════
    def enqueue_backfill(self, sid: int):
        self._set_status(sid, "pending")
        self._backfill_q.put(sid)

    def _scan_cursor(self):
        """回溯专用游标：独立连接、关闭 statement_timeout（后台长扫描允许超 30s）。
        不走连接池，避免把无超时设置泄漏给搜索/监控。"""
        if self._scan_conn is None or self._scan_conn.closed:
            self._scan_conn = psycopg2.connect(
                host=config.PG_HOST, port=config.PG_PORT, dbname=config.PG_DB,
                user=config.PG_USER, password=config.PG_PASSWORD,
                connect_timeout=10, options="-c statement_timeout=0",
            )
            self._scan_conn.autocommit = True
            log.info("回溯专用连接已建立（statement_timeout=0）")
        return self._scan_conn.cursor()

    def _backfill_worker(self):
        log.info("回溯 worker 启动")
        while self._running:
            try:
                sid = self._backfill_q.get(timeout=2)
            except queue.Empty:
                continue
            if self._suspend.is_set():
                self._backfill_q.task_done()
                time.sleep(1)
                continue
            try:
                self._active_backfill = sid
                self._run_backfill(sid)
            except Exception as e:
                log.error("回溯 #%d 异常: %s", sid, e, exc_info=True)
                self._set_status(sid, "failed", str(e)[:300])
            finally:
                self._active_backfill = None
                self._backfill_q.task_done()

    def _run_backfill(self, sid: int):
        # 取关键词与断点游标

        with pg_cursor() as cur:
            cur.execute("SELECT keyword, sync_cursor_ts, sync_cursor_hash FROM subs.subscriptions WHERE id=%s", (sid,))
            row = cur.fetchone()
        if not row:
            return
        keyword, cur_ts, cur_hash = row
        kw = f"%{keyword}%"
        cur_ts = cur_ts or "1970-01-01T00:00:00+00:00"
        cur_hash = cur_hash if cur_hash is not None else b""

        self._set_status(sid, "syncing")
        log.info("开始全量回溯: '%s' (id=%d)", keyword, sid)

        total_new = 0
        while self._running and not self._suspend.is_set():
            # 一条语句完成：批量匹配大库 → 防重写小库 → 写映射 → 推进游标
            # 用回溯专用连接（无超时），稀有词全表扫描不会被 30s 掉断
            cur = self._scan_cursor()
            try:
                cur.execute("""
                    WITH batch AS (
                        SELECT info_hash, name, size, files_count, created_at
                        FROM public.torrents
                        WHERE name ILIKE %(kw)s
                          AND (created_at, info_hash) > (%(cts)s::timestamptz, %(chash)s::bytea)
                        ORDER BY created_at, info_hash
                        LIMIT %(lim)s
                    ),
                    ins_t AS (
                        INSERT INTO subs.torrents (info_hash,name,size,files_count,created_at)
                        SELECT info_hash,name,size,files_count,created_at FROM batch
                        ON CONFLICT (info_hash) DO NOTHING
                        RETURNING info_hash
                    ),
                    ins_m AS (
                        INSERT INTO subs.subscription_matches (subscription_id, info_hash)
                        SELECT %(sid)s, info_hash FROM batch
                        ON CONFLICT DO NOTHING
                        RETURNING info_hash
                    )
                    SELECT
                      (SELECT count(*) FROM batch),
                      (SELECT count(*) FROM ins_t),
                      (SELECT max(created_at) FROM batch),
                      (SELECT encode(info_hash,'hex') FROM batch ORDER BY created_at DESC, info_hash DESC LIMIT 1)
                """, {"kw": kw, "cts": cur_ts, "chash": cur_hash, "lim": BACKFILL_BATCH, "sid": sid})
                batch_n, new_n, max_ts, max_hash = cur.fetchone()

                if batch_n == 0:
                    cur.close()
                    break

                cur_ts = max_ts
                cur_hash = bytes.fromhex(max_hash)
                total_new += new_n or 0

                cur.execute("""
                    UPDATE subs.subscriptions
                    SET sync_cursor_ts=%s, sync_cursor_hash=%s,
                        scanned_count = scanned_count + %s,
                        matched_count = matched_count + %s,
                        updated_at = now()
                    WHERE id=%s
                """, (cur_ts, cur_hash, batch_n, new_n or 0, sid))
            except Exception:
                # 连接可能已断，下次重连
                try:
                    if self._scan_conn and not self._scan_conn.closed:
                        self._scan_conn.close()
                except Exception:
                    pass
                self._scan_conn = None
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

            if batch_n < BACKFILL_BATCH:
                break
            time.sleep(0.05)   # 让出 CPU，弱机友好

        # 回溯完成 → monitoring；监控游标设到大库当前最新水位线
        with pg_cursor(autocommit=True) as cur:
            cur.execute("SELECT created_at, encode(info_hash,'hex') FROM public.torrents ORDER BY created_at DESC, info_hash DESC LIMIT 1")
            w = cur.fetchone()
            mon_ts = w[0] if w else None
            mon_hash = bytes.fromhex(w[1]) if w else None
            cur.execute("""
                UPDATE subs.subscriptions
                SET status='monitoring', monitor_cursor_ts=%s, monitor_cursor_hash=%s, updated_at=now()
                WHERE id=%s
            """, (mon_ts, mon_hash, sid))
        log.info("回溯完成: '%s' (id=%d) 新增 %d 条 → 转入监控", keyword, sid, total_new)

    # ══════════════════════════════════════════════════════════════
    # 4) 增量监控（monitoring）—— 单线程一次扫描，内存比对所有关键词
    # ══════════════════════════════════════════════════════════════
    def _monitor_loop(self):
        log.info("增量监控 worker 启动")
        while self._running:
            try:
                if not self._suspend.is_set():
                    self._monitor_once()
            except Exception as e:
                log.error("监控异常: %s", e, exc_info=True)
                time.sleep(MONITOR_INTERVAL * 2)
            time.sleep(MONITOR_INTERVAL)
        log.info("增量监控 worker 停止")

    def _monitor_once(self):
        # 取所有 monitoring 且启用的订阅

        with pg_cursor() as cur:
            cur.execute("""
                SELECT id, keyword, monitor_cursor_ts, monitor_cursor_hash, push_qb
                FROM subs.subscriptions
                WHERE status='monitoring' AND enabled=true
            """)
            subs = cur.fetchall()
        if not subs:
            return

        # 全局水位线 = 所有监控订阅里最旧的游标（一次扫描覆盖所有关键词）
        min_ts = min((s[2] for s in subs if s[2] is not None), default=None)
        min_hash = None
        for s in subs:
            if s[2] == min_ts:
                min_hash = s[3]
                break
        if min_ts is None:
            return
        min_hash = min_hash if min_hash is not None else b""

        # 一次拉取大库新增

        with pg_cursor() as cur:
            cur.execute("""
                SELECT encode(info_hash,'hex'), name, size, files_count, created_at
                FROM public.torrents
                WHERE (created_at, info_hash) > (%s::timestamptz, %s::bytea)
                ORDER BY created_at, info_hash
                LIMIT %s
            """, (min_ts, min_hash, MONITOR_BATCH))
            rows = cur.fetchall()
        if not rows:
            return

        # (sid, kw_lower, push_qb)
        kws = [(s[0], s[1].lower(), bool(s[4])) for s in subs]
        kw_by_sid = {s[0]: s[1] for s in subs}   # sid -> 原始关键词(精确推送校验用)
        last_ts, last_hash = rows[-1][4], rows[-1][0]

        inserted = 0
        with pg_cursor(autocommit=True) as cur:
            for hash_hex, name, size, files_count, created_at in rows:
                nl = (name or "").lower()
                matched = [(sid, push) for sid, kwl, push in kws if kwl in nl]
                if not matched:
                    continue
                cur.execute("""
                    INSERT INTO subs.torrents (info_hash,name,size,files_count,created_at)
                    VALUES (decode(%s,'hex'), %s, %s, %s, %s)
                    ON CONFLICT (info_hash) DO NOTHING
                """, (hash_hex, name, size, files_count, created_at))
                for sid, _push in matched:
                    cur.execute("""
                        INSERT INTO subs.subscription_matches (subscription_id, info_hash)
                        VALUES (%s, decode(%s,'hex')) ON CONFLICT DO NOTHING
                    """, (sid, hash_hex))
                    cur.execute("UPDATE subs.subscriptions SET matched_count=matched_count+1 WHERE id=%s", (sid,))
                inserted += 1
                # 仅「指定内容」订阅(push_qb=true) 且 名称精确匹配(整词/智能番号) 才推 qB；
                # 只是子串包含、未精确命中的不推。回溯历史永不推。
                push_matched = [sid for sid, push in matched
                                if push and self._precise_push_match(kw_by_sid.get(sid, ""), name)]
                if push_matched:
                    self._maybe_push_qb(hash_hex, name, size, push_matched)

            # 推进所有监控订阅的游标到本批最新
            cur.execute("""
                UPDATE subs.subscriptions
                SET monitor_cursor_ts=%s, monitor_cursor_hash=%s, updated_at=now()
                WHERE status='monitoring' AND enabled=true
            """, (last_ts, bytes.fromhex(last_hash)))

        if inserted:
            log.info("监控：本轮扫描 %d 条，命中入小库 %d 条", len(rows), inserted)

    def _maybe_push_qb(self, hash_hex, name, size, matched_sids):
        """可选：命中同时推送 qBittorrent（保留旧下载能力）。"""
        try:
            from qb_client import qb
            if qb.health_check():
                qb.add_magnet(hash_hex, name, category=getattr(config, "QB_CATEGORY", "radar"),
                              save_path=getattr(config, "QB_SAVE_PATH", ""))
        except Exception as e:
            log.warning("qB 推送失败 %s: %s", hash_hex[:12], e)

    # ══════════════════════════════════════════════════════════════
    # 清空 / 备份小库（供 WebUI 系统设置调用）
    # ══════════════════════════════════════════════════════════════
    def clear_all(self) -> dict:
        """清空小库（种子 + 映射 + 订阅）。只动 subs schema，绝不碰 bitmagnet 大库。
        先暂停后台 worker，避免边清边灌。"""
        self._suspend.set()
        time.sleep(2)   # 等当前批次结束
        # 清空回溯队列
        try:
            while True:
                self._backfill_q.get_nowait()
                self._backfill_q.task_done()
        except queue.Empty:
            pass
        try:
            with pg_cursor(autocommit=True) as cur:
                cur.execute("TRUNCATE subs.subscription_matches, subs.torrents, subs.subscriptions RESTART IDENTITY")
            log.info("小库已清空")
            return {"success": True, "message": "小库已清空（种子/映射/订阅）"}
        except Exception as e:
            log.error("清空小库失败: %s", e)
            return {"success": False, "message": str(e)[:300]}
        finally:
            self._suspend.clear()

    def init_schema(self):
        """幂等创建 subs schema（新设备部署时自动建表，不用手动跑 SQL）。"""
        try:
            with pg_cursor(autocommit=True) as cur:
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                except Exception as ex:
                    log.warning("pg_trgm 扩展创建失败(可能无权限/已存在): %s", ex)
                cur.execute("CREATE SCHEMA IF NOT EXISTS subs")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS subs.torrents (
                    info_hash    bytea       PRIMARY KEY,
                    name         text        NOT NULL,
                    size         bigint,
                    files_count  integer,
                    created_at   timestamptz,
                    first_seen   timestamptz NOT NULL DEFAULT now()
                )""")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS subs.subscriptions (
                    id                  bigserial   PRIMARY KEY,
                    keyword             text        NOT NULL UNIQUE,
                    status              text        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','syncing','monitoring','paused','failed')),
                    sync_cursor_ts      timestamptz DEFAULT '1970-01-01Z',
                    sync_cursor_hash    bytea       DEFAULT '\\x'::bytea,
                    monitor_cursor_ts   timestamptz,
                    monitor_cursor_hash bytea,
                    matched_count       bigint      NOT NULL DEFAULT 0,
                    scanned_count       bigint      NOT NULL DEFAULT 0,
                    last_error          text,
                    enabled             boolean     NOT NULL DEFAULT true,
                    created_at          timestamptz NOT NULL DEFAULT now(),
                    updated_at          timestamptz NOT NULL DEFAULT now(),
                    push_qb             boolean     NOT NULL DEFAULT false
                )""")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS subs.subscription_matches (
                    subscription_id bigint      NOT NULL REFERENCES subs.subscriptions(id) ON DELETE CASCADE,
                    info_hash       bytea       NOT NULL REFERENCES subs.torrents(info_hash) ON DELETE CASCADE,
                    matched_at      timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (subscription_id, info_hash)
                )""")
                cur.execute("CREATE INDEX IF NOT EXISTS subs_torrents_name_trgm_idx ON subs.torrents USING gin (name gin_trgm_ops)")
                cur.execute("CREATE INDEX IF NOT EXISTS subs_torrents_created_idx ON subs.torrents (created_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS subs_match_hash_idx ON subs.subscription_matches (info_hash)")
                # 兼容旧表列
                try:
                    cur.execute("ALTER TABLE subs.subscriptions ADD COLUMN IF NOT EXISTS push_qb boolean NOT NULL DEFAULT false")
                except Exception:
                    pass
                log.info("subs schema 初始化完成")
        except Exception as e:
            log.warning("subs schema 初始化失败(可能已存在): %s", e)

    def _pg_dump_via_network(self, path: str) -> tuple:
        """优先用网络 pg_dump（容器内/任意环境可用），不行退回 docker exec。"""
        pw = getattr(config, "PG_PASSWORD", "bitmagnet")
        host = getattr(config, "PG_HOST", "127.0.0.1")
        port = str(getattr(config, "PG_PORT", 5432))
        db = getattr(config, "PG_DB", "bitmagnet")
        user = getattr(config, "PG_USER", "bitmagnet")
        # 试网络 pg_dump
        env = os.environ.copy()
        env["PGPASSWORD"] = pw
        # 查找 pg_dump 路径 (可能叫 pg_dump, pg_dump-16 等)
        for pg in ["pg_dump", "pg_dump-16", "pg_dump-15", "pg_dump-14"]:
            try:
                p = subprocess.run(["which", pg], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3)
                if p.returncode == 0:
                    cmd = [pg, "-h", host, "-p", port, "-U", user, "-n", "subs", "-d", db, "--no-owner", "--no-acl"]
                    with open(path, "wb") as f:
                        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=1800, env=env)
                    if r.returncode == 0:
                        return (True, os.path.getsize(path))
                    else:
                        return (False, r.stderr.decode(errors="ignore")[:200] or "pg_dump 失败(returncode={})".format(r.returncode))
            except Exception:
                continue
        # 退回 docker exec
        try:
            cmd = ["docker", "exec", self._pg_container,
                   "pg_dump", "-U", user, "-n", "subs", "-d", db]
            with open(path, "wb") as f:
                p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=1800)
            if p.returncode == 0:
                return (True, os.path.getsize(path))
            return (False, p.stderr.decode(errors="ignore")[:200] or "docker exec pg_dump 失败")
        except Exception as e:
            return (False, str(e)[:200])

    def backup(self, path: str) -> dict:
        """备份 subs schema 到指定路径（网络 pg_dump 优先，兼容容器/宿主力）。"""
        path = (path or "").strip()
        if not path or not path.startswith("/"):
            return {"success": False, "message": "请提供绝对路径（以 / 开头）"}
        try:
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            ok, info = self._pg_dump_via_network(path)
            if ok:
                log.info("小库已备份 -> %s (%d bytes)", path, info)
                return {"success": True, "path": path, "size": info}
            return {"success": False, "message": str(info)[:300]}
        except Exception as e:
            log.error("备份异常: %s", e)
            return {"success": False, "message": str(e)[:300]}

    # ══════════════════════════════════════════════════════════════
    # 启停 + 启动恢复
    # ══════════════════════════════════════════════════════════════
    def start(self):
        if self._running:
            return
        self.init_schema()   # 开箱即用：自动建 subs schema
        self._running = True
        self._backfill_thread = threading.Thread(target=self._backfill_worker, name="SubsBackfill", daemon=True)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="SubsMonitor", daemon=True)
        self._backfill_thread.start()
        self._monitor_thread.start()
        # 恢复：未完成回溯的订阅重新入队
        try:
            with pg_cursor() as cur:
                cur.execute("SELECT id FROM subs.subscriptions WHERE status IN ('pending','syncing','failed') AND enabled=true ORDER BY id")
                ids = [r[0] for r in cur.fetchall()]
            for sid in ids:
                self._backfill_q.put(sid)
            if ids:
                log.info("启动恢复：%d 个订阅重新入回溯队列", len(ids))
        except Exception as e:
            log.warning("恢复回溯队列失败: %s", e)

    def stop(self):
        self._running = False

    # ══════════════════════════════════════════════════════════════
    # 前端兼容层：把订阅状态机映射成旧「挖掘/雷达/统计」形状
    # ══════════════════════════════════════════════════════════════
    # 订阅状态 → 旧 miner 状态
    _MINER_STATUS = {
        "pending": "pending", "syncing": "running",
        "monitoring": "done", "paused": "paused", "failed": "paused",
    }

    def miner_status(self, keyword: str = None) -> dict:
        """兼容旧 /api/miner/status：返回 {jobs:[...]}（回溯/监控进度）。"""
        subs = self.list_subscriptions()
        if keyword:
            subs = [s for s in subs if s["keyword"] == keyword]
        jobs = []
        for s in subs:
            st = self._MINER_STATUS.get(s["status"], "pending")
            # monitoring/done 视为已完成回溯 → 进度满；syncing 用已扫数占位
            est = s["scanned_count"] if s["status"] == "monitoring" else 0
            jobs.append({
                "keyword": s["keyword"], "status": st,
                "scanned_count": s["scanned_count"], "match_count": s["matched_count"],
                "estimated_total": est,
                "running": s["status"] in ("syncing",) or s["running"],
            })
        if keyword:
            return jobs[0] if jobs else {"found": False}
        return {"jobs": jobs}

    def monitor_status(self) -> dict:
        """兼容旧 /api/radar/status。"""

        with pg_cursor() as cur:
            cur.execute("SELECT count(*) FROM subs.subscriptions WHERE status='monitoring' AND enabled=true")
            mon = cur.fetchone()[0]
            cur.execute("""SELECT monitor_cursor_ts, encode(monitor_cursor_hash,'hex')
                           FROM subs.subscriptions WHERE monitor_cursor_ts IS NOT NULL
                           ORDER BY monitor_cursor_ts DESC LIMIT 1""")
            w = cur.fetchone()
        return {
            "running": self._running,
            "subscriptions": mon,
            "last_cursor_hash": (w[1] if w and w[1] else "-"),
            "watermark_ts": (w[0].isoformat() if w and w[0] else None),
            "interval_sec": MONITOR_INTERVAL,
        }

    def counts(self) -> dict:
        """兼容旧 /api/stats 的小库相关计数。"""

        with pg_cursor() as cur:
            cur.execute("SELECT count(*) FROM subs.torrents")
            small = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM subs.subscriptions")
            subs = cur.fetchone()[0]
        return {"small_torrents": small, "subscriptions": subs}

    # 按关键词操作（兼容旧 /api/miner/start|stop|delete）
    def start_by_keyword(self, keyword: str) -> dict:

        with pg_cursor() as cur:
            cur.execute("SELECT id FROM subs.subscriptions WHERE keyword=%s", (keyword,))
            row = cur.fetchone()
        if not row:
            return self.add_subscription(keyword)
        self.enqueue_backfill(row[0])
        return {"success": True, "message": f"已重新入队回溯: '{keyword}'"}

    def pause_by_keyword(self, keyword: str) -> dict:
        with pg_cursor(autocommit=True) as cur:
            cur.execute("UPDATE subs.subscriptions SET status='paused', updated_at=now() WHERE keyword=%s", (keyword,))
        return {"success": True, "message": f"已暂停: '{keyword}'"}

    def delete_by_keyword(self, keyword: str) -> dict:
        with pg_cursor(autocommit=True) as cur:
            cur.execute("DELETE FROM subs.subscriptions WHERE keyword=%s", (keyword,))
        return {"success": True, "message": f"已删除: '{keyword}'"}


subs_store = SubsStore()
